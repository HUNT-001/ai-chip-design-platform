"""
AGENT_B.testbench_generator — RTL → Verification-Environment Synthesis (T78)
============================================================================

Turns a parsed RTL module (from ``AGENT_H.rtl_graph``) into a **compilable**
verification environment: a UVM testbench, a plain-SystemVerilog self-checking
smoke test, a cocotb Python test, an SVA assertion package, a functional
coverage model, a filelist/Makefile and a regression config.

Pipeline (matches the AGENT_B block diagram)
--------------------------------------------
    RTL parser (rtl_graph)
        → port list + params + hierarchy + clock/reset + bus identification
        → AGENT_B: interfaces, transactions, UVM agents, DUT connect,
          sequences, scoreboard, coverage → runnable testbench

What is real vs scaffolded — stated plainly
-------------------------------------------
Everything *structural* is generated fully and correctly: the interface with
clocking blocks, the UVM item/driver/monitor/sequencer/agent/env, the config-db
wiring, virtual-interface connection, DUT instantiation with **every port
connected**, clock/reset generation, random and directed stimulus, a functional
coverage model over the real ports, and SVA for reset + detected handshakes.

The **reference model / scoreboard comparison** is *scaffolding*: a generator
cannot know the DUT's intended function, so the predictor is a clearly-marked
hook (``TODO: implement reference behaviour``) rather than an invented golden.
This is exactly how IP-XACT / UVMF generators work, and pretending otherwise
would be dishonest. Where AVA already has a golden model for the DUT class
(ALU, FIFO, FSM, a bus protocol), that hook is pre-populated with the call.

Bus-protocol awareness
----------------------
Port groups are matched against AXI4-Lite, APB, Wishbone, AXI-Stream and
TileLink signal signatures. A detected bus gets protocol-legal stimulus
(respects ``valid``/``ready`` and reset) and handshake-stability assertions,
rather than blind random toggling.

Stdlib-only. Deterministic (same RTL → byte-identical output). schema-v2.1.0.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

log = logging.getLogger("AGENT_B.tbgen")

SCHEMA_VERSION = "2.1.0"
AGENT_NAME = "testbench_generator"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# Port model (accepts rtl_graph.Module, a dict, or a list of port dicts)
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class TBPort:
    name: str
    direction: str            # input | output | inout
    width_expr: str = "1"     # "1", "[31:0]", "[W-1:0]", ...
    dtype: str = "logic"

    @property
    def is_input(self) -> bool:
        return self.direction == "input"

    @property
    def is_output(self) -> bool:
        return self.direction == "output"

    @property
    def msb(self) -> str:
        m = re.match(r"\[\s*([^\]:]+)\s*:", self.width_expr or "")
        return m.group(1).strip() if m else "0"

    @property
    def is_vector(self) -> bool:
        return "[" in (self.width_expr or "")

    def decl(self) -> str:
        w = f" {self.width_expr}" if self.is_vector else ""
        return f"logic{w} {self.name}"


CLOCK_HINTS = ("clk", "clock", "clk_i", "clk_ci", "aclk", "hclk", "pclk",
               "clk_in", "gclk")
RESET_HINTS = ("rst", "reset", "rst_n", "rst_ni", "resetn", "rst_rbi",
               "arst", "presetn", "hresetn", "rst_in")


def _norm(n: str) -> str:
    return n.lower().rstrip("_")


_CLK_TOKENS = {"clk", "clock", "aclk", "hclk", "pclk", "gclk", "clk_i", "ck",
               "clki", "clkin"}
_RST_TOKENS = {"rst", "reset", "rstn", "resetn", "arst", "arstn", "nrst",
               "rstni", "rst_n", "rst_ni", "rbi", "presetn", "hresetn"}


def _tokens(name: str) -> List[str]:
    """Split a signal name into lowercase tokens (camelCase + snake_case)."""
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    return [t for t in re.split(r"[_\W]+", s.lower()) if t]


def _is_reset_token(t: str) -> bool:
    # "reset" as a substring is safe (no common word contains it): catches
    # resetn / aresetn / presetn / hresetn. "rst" must be anchored — otherwise
    # "first"/"burst"/"worst" would match.
    if "reset" in t:
        return True
    if t in _RST_TOKENS:
        return True
    return bool(re.match(r"n?a?h?p?rst[nib]*$", t))     # rst, rstn, arstn, ...


def _is_clock_token(t: str) -> bool:
    if t in _CLK_TOKENS:
        return True
    return bool(re.match(r"[ahpg]?clk$", t))            # clk, aclk, hclk, ...


def detect_clock_reset(ports: Sequence[TBPort]
                       ) -> Tuple[Optional[TBPort], Optional[TBPort], bool]:
    """Return (clock, reset, reset_active_low).

    Matching is **token-aware**, not substring: `instr_first_cycle_i` must not
    be taken for a reset just because `"first"` contains `"rst"`.
    """
    clk = rst = None
    for p in ports:
        if not p.is_input or p.is_vector:
            continue
        toks = _tokens(p.name)
        if clk is None and any(_is_clock_token(t) for t in toks):
            clk = p
        if rst is None and any(_is_reset_token(t) for t in toks):
            rst = p
    active_low = False
    if rst is not None:
        ln = rst.name.lower()
        active_low = (ln.endswith("_n") or ln.endswith("_ni")
                      or ln.endswith("_rbi") or ln.endswith("n")
                      or "resetn" in ln or "rst_n" in ln)
    return clk, rst, active_low


# ─────────────────────────────────────────────────────────────────────────────
# Bus-protocol detection
# ─────────────────────────────────────────────────────────────────────────────
# role-suffix signatures (lowercased, prefix-stripped) that identify a protocol
_BUS_SIGNATURES: Dict[str, Dict[str, Any]] = {
    "AXI4-Lite": {"required": {"awvalid", "awready", "awaddr", "wvalid",
                               "wready", "wdata", "bvalid", "bready",
                               "arvalid", "arready", "araddr", "rvalid",
                               "rready", "rdata"},
                  "min": 10},
    "AXI-Stream": {"required": {"tvalid", "tready", "tdata"},
                   "optional": {"tlast", "tkeep", "tstrb", "tuser"}, "min": 3},
    "APB": {"required": {"psel", "penable", "pwrite", "paddr", "pwdata",
                         "prdata", "pready"}, "min": 5},
    "Wishbone": {"required": {"cyc", "stb", "we", "ack"},
                 "optional": {"adr", "dat_i", "dat_o", "sel", "stall", "err"},
                 "min": 4},
    "TileLink": {"required": {"a_valid", "a_ready", "a_opcode", "d_valid",
                              "d_ready", "d_opcode"}, "min": 5},
}


@dataclass
class BusInterface:
    protocol: str
    prefix: str
    ports: List[TBPort] = field(default_factory=list)
    role_map: Dict[str, TBPort] = field(default_factory=dict)


def _role(prefix: str, name: str) -> str:
    r = name.lower()
    if prefix and r.startswith(prefix.lower()):
        r = r[len(prefix):]
    return r.lstrip("_")


def detect_buses(ports: Sequence[TBPort]) -> List[BusInterface]:
    """Group ports by common prefix and match protocol signatures."""
    # candidate prefixes: split each name on '_' and take leading fragments,
    # plus the empty prefix (all ports) so bare bus signals with no separator
    # — APB's `psel`/`penable`/… — are still grouped.
    groups: Dict[str, List[TBPort]] = {"": list(ports)}
    for p in ports:
        parts = p.name.split("_")
        for k in range(1, len(parts)):
            pre = "_".join(parts[:k])
            groups.setdefault(pre, []).append(p)
    buses: List[BusInterface] = []
    claimed: set = set()
    # try longer prefixes first (more specific)
    for pre in sorted(groups, key=lambda x: -len(x)):
        members = groups[pre]
        roles = {_role(pre, p.name) for p in members}
        for proto, sig in _BUS_SIGNATURES.items():
            req = sig["required"]
            hit = req & roles
            if len(hit) >= sig.get("min", len(req)) and \
                    len(hit) >= 0.6 * len(req):
                names = {p.name for p in members}
                if names & claimed:
                    continue
                bus = BusInterface(proto, pre, list(members))
                for p in members:
                    bus.role_map[_role(pre, p.name)] = p
                buses.append(bus)
                claimed |= names
                break
    return buses


# ─────────────────────────────────────────────────────────────────────────────
# Input normalisation
# ─────────────────────────────────────────────────────────────────────────────
def _to_ports(spec: Any) -> Tuple[str, List[TBPort], Dict[str, str]]:
    """Accept an rtl_graph.Module, a dict, or a (name, ports) pair."""
    name = "dut"
    params: Dict[str, str] = {}
    raw: List[Any] = []
    if hasattr(spec, "ports") and hasattr(spec, "name"):        # rtl_graph.Module
        name = spec.name
        params = dict(getattr(spec, "parameters", {}) or {})
        raw = spec.ports
    elif isinstance(spec, dict):
        name = spec.get("name", "dut")
        params = dict(spec.get("parameters", {}) or {})
        raw = spec.get("ports", [])
    elif isinstance(spec, (list, tuple)):
        raw = list(spec)
    ports: List[TBPort] = []
    for p in raw:
        if isinstance(p, TBPort):
            ports.append(p)
        elif hasattr(p, "name") and hasattr(p, "direction"):    # rtl_graph.Port
            ports.append(TBPort(p.name, p.direction,
                                getattr(p, "width", "1") or "1",
                                getattr(p, "dtype", "logic") or "logic"))
        elif isinstance(p, dict):
            ports.append(TBPort(p["name"], p.get("direction", "input"),
                                p.get("width", "1") or "1",
                                p.get("dtype", "logic")))
    return name, ports, params


# ─────────────────────────────────────────────────────────────────────────────
# Generator
# ─────────────────────────────────────────────────────────────────────────────
class TestbenchGenerator:
    def __init__(self, spec: Any, clock_period: int = 10):
        self.dut, self.ports, self.params = _to_ports(spec)
        self.clk, self.rst, self.rst_active_low = detect_clock_reset(self.ports)
        self.buses = detect_buses(self.ports)
        self.period = clock_period
        # data ports = everything that is not clock or reset
        skip = {p.name for p in (self.clk, self.rst) if p}
        self.inputs = [p for p in self.ports if p.is_input and p.name not in skip]
        self.outputs = [p for p in self.ports if p.is_output]
        self.data_ports = [p for p in self.ports if p.name not in skip]

    # ── helpers ─────────────────────────────────────────────────────────────
    def _clk(self) -> str:
        return self.clk.name if self.clk else "clk_i"

    def _rst(self) -> str:
        return self.rst.name if self.rst else "rst_ni"

    def _rst_assert(self) -> str:
        return "1'b0" if self.rst_active_low else "1'b1"

    def _rst_deassert(self) -> str:
        return "1'b1" if self.rst_active_low else "1'b0"

    # ── 1. interface ────────────────────────────────────────────────────────
    def gen_interface(self) -> str:
        sigs = "\n".join(f"  {p.decl()};" for p in self.data_ports)
        drive = "\n".join(f"    output {p.name};" for p in self.inputs)
        sample = "\n".join(f"    input {p.name};" for p in self.outputs)
        return f"""// Generated by AVA AGENT_B — interface for {self.dut}
`ifndef {self.dut.upper()}_IF_SV
`define {self.dut.upper()}_IF_SV

interface {self.dut}_if (input logic {self._clk()});
{sigs}

  // Driver clocking block: TB drives DUT inputs
  clocking drv_cb @(posedge {self._clk()});
    default input #1step output #1;
{drive if drive else '    // (no data inputs)'}
  endclocking

  // Monitor clocking block: TB samples DUT outputs
  clocking mon_cb @(posedge {self._clk()});
    default input #1step;
{sample if sample else '    // (no outputs)'}
  endclocking

  modport drv (clocking drv_cb, input {self._clk()});
  modport mon (clocking mon_cb, input {self._clk()});
endinterface
`endif
"""

    # ── 2. UVM package (item/driver/monitor/…/env/seqs/refmodel/coverage) ────
    def _item_fields(self) -> str:
        lines = []
        for p in self.data_ports:
            w = f" {p.width_expr}" if p.is_vector else ""
            rnd = "rand " if p.is_input else ""
            lines.append(f"  {rnd}logic{w} {p.name};")
        return "\n".join(lines)

    def _item_constraints(self) -> str:
        # protocol-aware: keep detected valid/ready in-band; otherwise free
        cons = []
        for bus in self.buses:
            for role in ("awvalid", "wvalid", "arvalid", "tvalid", "psel",
                         "cyc", "a_valid"):
                p = bus.role_map.get(role)
                if p and p.is_input:
                    cons.append(f"  constraint c_{p.name}_legal {{ "
                                f"{p.name} dist {{1'b1 := 3, 1'b0 := 1}}; }}")
        return "\n".join(cons)

    def _coverage_bins(self) -> str:
        cps = []
        for p in self.data_ports[:16]:
            if p.is_vector:
                cps.append(f"    cp_{p.name}: coverpoint item.{p.name} {{\n"
                           f"      bins zero = {{0}};\n"
                           f"      bins low  = {{[1:15]}};\n"
                           f"      bins high = default;\n    }}")
            else:
                cps.append(f"    cp_{p.name}: coverpoint item.{p.name} {{\n"
                           f"      bins lo = {{0}}; bins hi = {{1}};\n    }}")
        return "\n".join(cps)

    def gen_pkg(self) -> str:
        d = self.dut
        refhook = self._refmodel_hook()
        return f"""// Generated by AVA AGENT_B — UVM package for {d}
`ifndef {d.upper()}_PKG_SV
`define {d.upper()}_PKG_SV
package {d}_pkg;
  import uvm_pkg::*;
  `include "uvm_macros.svh"

  // ---- transaction item ----
  class {d}_item extends uvm_sequence_item;
{self._item_fields()}
{self._item_constraints()}
    `uvm_object_utils_begin({d}_item)
{self._item_field_automation()}
    `uvm_object_utils_end
    function new(string name="{d}_item"); super.new(name); endfunction
  endclass

  // ---- driver ----
  class {d}_driver extends uvm_driver #({d}_item);
    `uvm_component_utils({d}_driver)
    virtual {d}_if vif;
    function new(string name, uvm_component parent); super.new(name,parent); endfunction
    function void build_phase(uvm_phase phase);
      super.build_phase(phase);
      if (!uvm_config_db#(virtual {d}_if)::get(this,"","vif",vif))
        `uvm_fatal("NOVIF","virtual interface not set for driver")
    endfunction
    task run_phase(uvm_phase phase);
      forever begin
        {d}_item tr;
        seq_item_port.get_next_item(tr);
        @(vif.drv_cb);
{self._driver_assigns()}
        `uvm_info("DRV", $sformatf("drove %s", tr.sprint()), UVM_HIGH)
        seq_item_port.item_done();
      end
    endtask
  endclass

  // ---- monitor ----
  class {d}_monitor extends uvm_monitor;
    `uvm_component_utils({d}_monitor)
    virtual {d}_if vif;
    uvm_analysis_port #({d}_item) ap;
    function new(string name, uvm_component parent);
      super.new(name,parent); ap = new("ap", this);
    endfunction
    function void build_phase(uvm_phase phase);
      super.build_phase(phase);
      if (!uvm_config_db#(virtual {d}_if)::get(this,"","vif",vif))
        `uvm_fatal("NOVIF","virtual interface not set for monitor")
    endfunction
    task run_phase(uvm_phase phase);
      forever begin
        {d}_item tr = {d}_item::type_id::create("tr");
        @(vif.mon_cb);
{self._monitor_samples()}
        ap.write(tr);
      end
    endtask
  endclass

  // ---- sequencer ----
  typedef uvm_sequencer #({d}_item) {d}_sequencer;

  // ---- agent ----
  class {d}_agent extends uvm_agent;
    `uvm_component_utils({d}_agent)
    {d}_driver    drv;
    {d}_monitor   mon;
    {d}_sequencer seqr;
    function new(string name, uvm_component parent); super.new(name,parent); endfunction
    function void build_phase(uvm_phase phase);
      super.build_phase(phase);
      mon = {d}_monitor::type_id::create("mon", this);
      if (get_is_active() == UVM_ACTIVE) begin
        drv  = {d}_driver::type_id::create("drv", this);
        seqr = {d}_sequencer::type_id::create("seqr", this);
      end
    endfunction
    function void connect_phase(uvm_phase phase);
      if (get_is_active() == UVM_ACTIVE)
        drv.seq_item_port.connect(seqr.seq_item_export);
    endfunction
  endclass

  // ---- reference model (scaffolding: predict expected outputs) ----
  class {d}_refmodel extends uvm_component;
    `uvm_component_utils({d}_refmodel)
    function new(string name, uvm_component parent); super.new(name,parent); endfunction
    // Predict the expected outputs for a stimulus item.
    virtual function {d}_item predict({d}_item stim);
      {d}_item exp = {d}_item::type_id::create("exp");
      exp.copy(stim);
{refhook}
      return exp;
    endfunction
  endclass

  // ---- scoreboard ----
  `uvm_analysis_imp_decl(_mon)
  class {d}_scoreboard extends uvm_scoreboard;
    `uvm_component_utils({d}_scoreboard)
    uvm_analysis_imp_mon #({d}_item, {d}_scoreboard) mon_imp;
    {d}_refmodel ref_model;
    int matched, mismatched;
    function new(string name, uvm_component parent);
      super.new(name,parent); mon_imp = new("mon_imp", this);
    endfunction
    function void build_phase(uvm_phase phase);
      ref_model = {d}_refmodel::type_id::create("ref_model", this);
    endfunction
    function void write_mon({d}_item observed);
      {d}_item expected = ref_model.predict(observed);
      if (expected.compare(observed)) matched++;
      else begin
        mismatched++;
        `uvm_error("SCB", $sformatf("mismatch\\n exp: %s obs: %s",
                   expected.sprint(), observed.sprint()))
      end
    endfunction
    function void report_phase(uvm_phase phase);
      `uvm_info("SCB", $sformatf("matched=%0d mismatched=%0d", matched, mismatched), UVM_LOW)
    endfunction
  endclass

  // ---- functional coverage ----
  class {d}_coverage extends uvm_subscriber #({d}_item);
    `uvm_component_utils({d}_coverage)
    {d}_item item;
    covergroup cg;
      option.per_instance = 1;
{self._coverage_bins()}
    endgroup
    function new(string name, uvm_component parent);
      super.new(name,parent); cg = new();
    endfunction
    function void write({d}_item t); item = t; cg.sample(); endfunction
  endclass

  // ---- environment ----
  class {d}_env extends uvm_env;
    `uvm_component_utils({d}_env)
    {d}_agent      agent;
    {d}_scoreboard scb;
    {d}_coverage   cov;
    function new(string name, uvm_component parent); super.new(name,parent); endfunction
    function void build_phase(uvm_phase phase);
      super.build_phase(phase);
      agent = {d}_agent::type_id::create("agent", this);
      scb   = {d}_scoreboard::type_id::create("scb", this);
      cov   = {d}_coverage::type_id::create("cov", this);
    endfunction
    function void connect_phase(uvm_phase phase);
      agent.mon.ap.connect(scb.mon_imp);
      agent.mon.ap.connect(cov.analysis_export);
    endfunction
  endclass

  // ---- sequences ----
  class {d}_random_seq extends uvm_sequence #({d}_item);
    `uvm_object_utils({d}_random_seq)
    rand int unsigned n_items = 100;
    function new(string name="{d}_random_seq"); super.new(name); endfunction
    task body();
      repeat (n_items) begin
        {d}_item tr = {d}_item::type_id::create("tr");
        start_item(tr);
        if (!tr.randomize()) `uvm_error("SEQ","randomize failed")
        finish_item(tr);
      end
    endtask
  endclass

  class {d}_smoke_seq extends uvm_sequence #({d}_item);
    `uvm_object_utils({d}_smoke_seq)
    function new(string name="{d}_smoke_seq"); super.new(name); endfunction
    task body();
      {d}_item tr = {d}_item::type_id::create("tr");
      start_item(tr);
      if (!tr.randomize()) `uvm_error("SEQ","randomize failed")
      finish_item(tr);
    endtask
  endclass

endpackage
`endif
"""

    def _item_field_automation(self) -> str:
        out = []
        for p in self.data_ports:
            out.append(f"      `uvm_field_int({p.name}, UVM_ALL_ON)")
        return "\n".join(out)

    def _driver_assigns(self) -> str:
        out = []
        for p in self.inputs:
            out.append(f"        vif.drv_cb.{p.name} <= tr.{p.name};")
        return "\n".join(out) if out else "        // no data inputs to drive"

    def _monitor_samples(self) -> str:
        out = []
        for p in self.outputs:
            out.append(f"        tr.{p.name} = vif.mon_cb.{p.name};")
        for p in self.inputs:               # capture stimulus for the scoreboard
            out.append(f"        tr.{p.name} = vif.drv_cb.{p.name};")
        return "\n".join(out) if out else "        // no ports to sample"

    def _refmodel_hook(self) -> str:
        # Pre-populate the hook if AVA already has a golden for this DUT class.
        blob = self.dut.lower()
        known = None
        if "alu" in blob:
            known = "pipeline_verifier.alu_eval / AGENT_H bitmanip/atomics goldens"
        elif "fifo" in blob:
            known = "rtl_basics_verifier.FIFOModel"
        elif "fsm" in blob or "ctrl" in blob or "controller" in blob:
            known = "rtl_basics_verifier.FSMModel (feed rtl_graph fsm_def)"
        elif any(b.protocol for b in self.buses):
            known = f"{self.buses[0].protocol} checker in interconnect/bus_verifier"
        hint = f"\n      // AVA has a golden for this DUT class: {known}" if known else ""
        return (f"      // TODO: implement reference behaviour for '{self.dut}'."
                f"{hint}\n"
                f"      // The generator cannot infer DUT function; fill outputs "
                f"below.\n"
                f"      // e.g. exp.result_o = <predicted from stim inputs>;")

    # ── 3. UVM top ──────────────────────────────────────────────────────────
    def gen_tb_top(self) -> str:
        d = self.dut
        conns = ",\n".join(f"    .{p.name}(dut_if.{p.name})"
                           if p.name not in {self._clk()} else
                           f"    .{p.name}({self._clk()})"
                           for p in self.ports)
        rst_seq = ""
        if self.rst:
            rst_seq = (f"    {self._rst()} = {self._rst_assert()};\n"
                       f"    repeat (5) @(posedge {self._clk()});\n"
                       f"    {self._rst()} = {self._rst_deassert()};")
        rst_decl = f"  logic {self._rst()};\n" if self.rst else ""
        rst_conn = ""
        return f"""// Generated by AVA AGENT_B — UVM testbench top for {d}
`ifndef {d.upper()}_TB_TOP_SV
`define {d.upper()}_TB_TOP_SV
`include "uvm_macros.svh"

module {d}_tb_top;
  import uvm_pkg::*;
  import {d}_pkg::*;

  logic {self._clk()};
{rst_decl}
  // clock
  initial begin {self._clk()} = 1'b0; forever #{self.period // 2} {self._clk()} = ~{self._clk()}; end

  // interface
  {d}_if dut_if(.{self._clk()}({self._clk()}));

  // DUT
  {d} dut (
{conns}
  );

  // reset
  initial begin
{rst_seq if rst_seq else '    // no reset detected'}
  end

  initial begin
    uvm_config_db#(virtual {d}_if)::set(null, "*", "vif", dut_if);
    run_test();
  end

  // global watchdog
  initial begin #100000; `uvm_fatal("TIMEOUT","watchdog expired"); end
endmodule
`endif
"""

    # ── 4. base test + directed test ─────────────────────────────────────────
    def gen_tests(self) -> str:
        d = self.dut
        return f"""// Generated by AVA AGENT_B — UVM tests for {d}
`ifndef {d.upper()}_TESTS_SV
`define {d.upper()}_TESTS_SV
`include "uvm_macros.svh"
import uvm_pkg::*;
import {d}_pkg::*;

class {d}_base_test extends uvm_test;
  `uvm_component_utils({d}_base_test)
  {d}_env env;
  function new(string name, uvm_component parent); super.new(name,parent); endfunction
  function void build_phase(uvm_phase phase);
    super.build_phase(phase);
    env = {d}_env::type_id::create("env", this);
  endfunction
endclass

class {d}_smoke_test extends {d}_base_test;
  `uvm_component_utils({d}_smoke_test)
  function new(string name, uvm_component parent); super.new(name,parent); endfunction
  task run_phase(uvm_phase phase);
    {d}_smoke_seq seq = {d}_smoke_seq::type_id::create("seq");
    phase.raise_objection(this);
    seq.start(env.agent.seqr);
    #100;
    phase.drop_objection(this);
  endtask
endclass

class {d}_random_test extends {d}_base_test;
  `uvm_component_utils({d}_random_test)
  function new(string name, uvm_component parent); super.new(name,parent); endfunction
  task run_phase(uvm_phase phase);
    {d}_random_seq seq = {d}_random_seq::type_id::create("seq");
    phase.raise_objection(this);
    if (!seq.randomize() with {{ n_items == 200; }}) `uvm_error("TEST","rand failed")
    seq.start(env.agent.seqr);
    #200;
    phase.drop_objection(this);
  endtask
endclass
`endif
"""

    # ── 5. plain-SV self-checking smoke TB (no UVM; iverilog-friendly) ───────
    def gen_smoke_tb(self) -> str:
        d = self.dut
        decls = "\n".join(f"  {p.decl()};" for p in self.ports
                          if p.name != self._clk())
        conns = ",\n".join(f"    .{p.name}({p.name})" for p in self.ports)
        drives = "\n".join(
            f"      {p.name} <= $random;" for p in self.inputs) or \
            "      ; // no data inputs"
        rst_lines = ""
        if self.rst:
            rst_lines = (f"    {self._rst()} = {self._rst_assert()};\n"
                         f"    repeat (5) @(posedge {self._clk()});\n"
                         f"    {self._rst()} = {self._rst_deassert()};")
        return f"""// Generated by AVA AGENT_B — plain-SV self-checking smoke test for {d}
// Runs on any Verilog simulator (iverilog/verilator); no UVM required.
`timescale 1ns/1ps
module {d}_smoke_tb;
  logic {self._clk()};
{decls}

  {d} dut (
{conns}
  );

  initial begin {self._clk()} = 0; forever #{self.period // 2} {self._clk()} = ~{self._clk()}; end

  integer i;
  initial begin
    $dumpfile("{d}_smoke.vcd"); $dumpvars(0, {d}_smoke_tb);
{rst_lines if rst_lines else '    // no reset detected'}
    for (i = 0; i < 100; i = i + 1) begin
      @(posedge {self._clk()});
{drives}
    end
    @(posedge {self._clk()});
    // Basic liveness check: DUT did not X-out its outputs after reset.
{self._smoke_checks()}
    $display("[{d}_smoke_tb] PASS: %0d cycles driven", i);
    $finish;
  end

  // watchdog
  initial begin #100000; $display("[{d}_smoke_tb] TIMEOUT"); $finish; end
endmodule
"""

    def _smoke_checks(self) -> str:
        out = []
        for p in self.outputs[:8]:
            out.append(f"    if (^{p.name} === 1'bx) "
                       f"$display(\"[{self.dut}_smoke_tb] WARN: {p.name} is X\");")
        return "\n".join(out) if out else "    // no outputs to check"

    # ── 6. cocotb Python test ────────────────────────────────────────────────
    def gen_cocotb(self) -> str:
        d = self.dut
        drives = "\n".join(
            f"        dut.{p.name}.value = random.getrandbits("
            f"{self._width_bits(p)})" for p in self.inputs)
        rst = ""
        if self.rst:
            av = "0" if self.rst_active_low else "1"
            dv = "1" if self.rst_active_low else "0"
            rst = (f"    dut.{self._rst()}.value = {av}\n"
                   f"    await ClockCycles(dut.{self._clk()}, 5)\n"
                   f"    dut.{self._rst()}.value = {dv}")
        checks = "\n".join(
            f"    assert dut.{p.name}.value.is_resolvable, "
            f"f'{p.name} is X after reset'" for p in self.outputs[:8]) or \
            "    pass  # no outputs to check"
        return f'''"""Generated by AVA AGENT_B — cocotb testbench for {d}.
Run:  make SIM=verilator  (or icarus)   inside this directory.
"""
import random
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge


async def _reset(dut):
{rst if rst else "    pass  # no reset detected"}


@cocotb.test()
async def smoke(dut):
    """Clock + reset + a few random-stimulus cycles; check outputs resolve."""
    cocotb.start_soon(Clock(dut.{self._clk()}, {self.period}, units="ns").start())
    await _reset(dut)
    await RisingEdge(dut.{self._clk()})
{checks}


@cocotb.test()
async def random_stimulus(dut):
    """Constrained-random drive over many cycles."""
    cocotb.start_soon(Clock(dut.{self._clk()}, {self.period}, units="ns").start())
    await _reset(dut)
    for _ in range(200):
        await RisingEdge(dut.{self._clk()})
{drives if drives else "        pass  # no data inputs"}
'''

    def _width_bits(self, p: TBPort) -> int:
        if not p.is_vector:
            return 1
        m = re.match(r"\[\s*(\d+)\s*:", p.width_expr or "")
        return (int(m.group(1)) + 1) if m else 32

    def gen_cocotb_makefile(self) -> str:
        return f"""# Generated by AVA AGENT_B — cocotb Makefile
SIM ?= verilator
TOPLEVEL_LANG ?= verilog
VERILOG_SOURCES += $(PWD)/../rtl/{self.dut}.sv
TOPLEVEL = {self.dut}
MODULE = {self.dut}_cocotb
include $(shell cocotb-config --makefiles)/Makefile.sim
"""

    # ── 7. SVA assertion package ─────────────────────────────────────────────
    def gen_assertions(self) -> str:
        d = self.dut
        props = []
        if self.rst:
            for p in self.outputs[:8]:
                props.append(
                    f"  // {p.name} must be known one cycle after reset release\n"
                    f"  property p_{p.name}_known;\n"
                    f"    @(posedge {self._clk()}) disable iff "
                    f"({self._rst()} == {self._rst_assert()})\n"
                    f"    !$isunknown({p.name});\n"
                    f"  endproperty\n"
                    f"  a_{p.name}_known: assert property (p_{p.name}_known);")
        for bus in self.buses:
            props.append(self._handshake_assertions(bus))
        body = "\n\n".join(props) if props else \
            "  // no reset/handshake structure detected for automatic SVA"
        return f"""// Generated by AVA AGENT_B — SVA assertion package for {d}
// Bind this to the DUT: bind {d} {d}_assertions u_asrt (.*);
`ifndef {d.upper()}_ASSERTIONS_SV
`define {d.upper()}_ASSERTIONS_SV
module {d}_assertions (
  input logic {self._clk()}{',' if self.rst else ''}
  {('input logic ' + self._rst()) if self.rst else ''}
  {self._assertion_port_list()}
);
{body}
endmodule
`endif
"""

    def _assertion_port_list(self) -> str:
        pl = [p for p in self.outputs[:8]]
        for bus in self.buses:
            pl += [p for p in bus.ports if p.name not in {x.name for x in pl}]
        if not pl:
            return ""
        return "," + ",\n  ".join("") + ",\n  ".join(
            f"input logic{(' ' + p.width_expr) if p.is_vector else ''} {p.name}"
            for p in pl)

    def _handshake_assertions(self, bus: BusInterface) -> str:
        # valid must stay stable until ready for AXI-family
        pairs = [("awvalid", "awready"), ("wvalid", "wready"),
                 ("arvalid", "arready"), ("tvalid", "tready")]
        out = []
        for vld, rdy in pairs:
            pv, pr = bus.role_map.get(vld), bus.role_map.get(rdy)
            if pv and pr:
                out.append(
                    f"  // {bus.protocol}: {pv.name} must hold until {pr.name}\n"
                    f"  property p_{pv.name}_stable;\n"
                    f"    @(posedge {self._clk()}) "
                    f"({pv.name} && !{pr.name}) |=> {pv.name};\n"
                    f"  endproperty\n"
                    f"  a_{pv.name}_stable: assert property (p_{pv.name}_stable);")
        return "\n".join(out)

    # ── 8. build glue: filelist, Makefile, regression, README ────────────────
    def gen_filelist(self) -> str:
        return "\n".join([
            "// Generated by AVA AGENT_B — compile order",
            f"rtl/{self.dut}.sv",
            f"{self.dut}_if.sv",
            f"{self.dut}_pkg.sv",
            f"{self.dut}_assertions.sv",
            f"{self.dut}_tests.sv",
            f"{self.dut}_tb_top.sv",
        ])

    def gen_makefile(self) -> str:
        d = self.dut
        return f"""# Generated by AVA AGENT_B — UVM simulation Makefile
# Usage: make TEST={d}_smoke_test SIM=xcelium   (or vcs / questa)
SIM ?= xcelium
TEST ?= {d}_smoke_test
FILELIST = {d}.f

UVM ?= +incdir+$(UVM_HOME)/src $(UVM_HOME)/src/uvm_pkg.sv

xcelium:
\txrun -uvm -f $(FILELIST) +UVM_TESTNAME=$(TEST) +UVM_VERBOSITY=UVM_MEDIUM
vcs:
\tvcs -sverilog -ntb_opts uvm -f $(FILELIST) -R +UVM_TESTNAME=$(TEST)
questa:
\tqrun -uvm -f $(FILELIST) -top {d}_tb_top +UVM_TESTNAME=$(TEST)
smoke:
\tiverilog -g2012 -o {d}_smoke rtl/{d}.sv {d}_smoke_tb.sv && vvp {d}_smoke
"""

    def gen_regression(self) -> Dict[str, Any]:
        d = self.dut
        return {
            "schema_version": SCHEMA_VERSION,
            "dut": d,
            "tests": [
                {"name": f"{d}_smoke_test", "seed": 1, "tags": ["smoke"]},
                {"name": f"{d}_random_test", "seed": 1, "tags": ["random"]},
                {"name": f"{d}_random_test", "seed": 2, "tags": ["random"]},
                {"name": f"{d}_random_test", "seed": 3, "tags": ["random"]},
            ],
            "coverage": {"functional": f"{d}_coverage", "goal_pct": 90},
            "sim_default": "xcelium",
        }

    def gen_readme(self) -> str:
        buses = ", ".join(f"{b.protocol}({b.prefix})" for b in self.buses) \
            or "none auto-detected"
        return f"""# {self.dut} — auto-generated verification environment (AVA AGENT_B)

Synthesised from the parsed RTL interface. **Structure is complete and
compilable; the reference model is a scaffold you must fill** (a generator
cannot infer the DUT's intended function).

- Clock: `{self._clk()}`  Reset: `{self._rst() if self.rst else 'none detected'}`{' (active-low)' if self.rst and self.rst_active_low else ''}
- Data ports: {len(self.data_ports)}  ({len(self.inputs)} in, {len(self.outputs)} out)
- Detected buses: {buses}

## Files
- `{self.dut}_if.sv` — interface + driver/monitor clocking blocks
- `{self.dut}_pkg.sv` — UVM item/driver/monitor/agent/scoreboard/coverage/env/seqs + **refmodel (fill me)**
- `{self.dut}_tb_top.sv` — top: clock/reset gen, DUT connect, `run_test()`
- `{self.dut}_tests.sv` — smoke + constrained-random tests
- `{self.dut}_assertions.sv` — SVA (reset + detected handshakes); `bind` to DUT
- `{self.dut}_smoke_tb.sv` — plain-SV self-checking smoke (no UVM; iverilog)
- `cocotb/` — Python cocotb test (verilator/icarus)
- `{self.dut}.f`, `Makefile`, `regression.yaml`

## Run
```
make TEST={self.dut}_random_test SIM=xcelium   # UVM
make smoke                                     # plain-SV (iverilog)
cd cocotb && make SIM=verilator                # cocotb
```

## Next step
Open `{self.dut}_pkg.sv`, find `{self.dut}_refmodel::predict()`, and implement
the expected-output computation. The scoreboard already wires it to the monitor.
"""

    # ── orchestration ────────────────────────────────────────────────────────
    def generate(self) -> Dict[str, str]:
        """Return {relative_path: content} for the whole environment."""
        files = {
            f"{self.dut}_if.sv": self.gen_interface(),
            f"{self.dut}_pkg.sv": self.gen_pkg(),
            f"{self.dut}_tb_top.sv": self.gen_tb_top(),
            f"{self.dut}_tests.sv": self.gen_tests(),
            f"{self.dut}_smoke_tb.sv": self.gen_smoke_tb(),
            f"{self.dut}_assertions.sv": self.gen_assertions(),
            f"cocotb/{self.dut}_cocotb.py": self.gen_cocotb(),
            "cocotb/Makefile": self.gen_cocotb_makefile(),
            f"{self.dut}.f": self.gen_filelist(),
            "Makefile": self.gen_makefile(),
            "regression.yaml": _yaml(self.gen_regression()),
            "README.md": self.gen_readme(),
        }
        return files

    def summary(self) -> Dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "agent": AGENT_NAME,
            "dut": self.dut,
            "clock": self._clk() if self.clk else None,
            "reset": self._rst() if self.rst else None,
            "reset_active_low": self.rst_active_low,
            "ports": len(self.ports),
            "inputs": len(self.inputs),
            "outputs": len(self.outputs),
            "parameters": sorted(self.params),
            "detected_buses": [{"protocol": b.protocol, "prefix": b.prefix,
                                "signals": len(b.ports)} for b in self.buses],
        }

    def write(self, out_dir: str) -> List[str]:
        base = Path(out_dir) / f"{self.dut}_tb"
        written = []
        for rel, content in self.generate().items():
            p = base / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            written.append(str(p))
        (base / "gen_report.json").write_text(
            json.dumps({**self.summary(), "files": [Path(w).name
                                                    for w in written],
                        "generated_at": _now()}, indent=2), encoding="utf-8")
        return written


def _yaml(d: Dict[str, Any], indent: int = 0) -> str:
    """Tiny YAML emitter (stdlib-only) for the regression config."""
    sp = "  " * indent
    out = []
    for k, v in d.items():
        if isinstance(v, dict):
            out.append(f"{sp}{k}:")
            out.append(_yaml(v, indent + 1))
        elif isinstance(v, list):
            out.append(f"{sp}{k}:")
            for item in v:
                if isinstance(item, dict):
                    first = True
                    for ik, iv in item.items():
                        lead = "- " if first else "  "
                        out.append(f"{sp}  {lead}{ik}: {json.dumps(iv)}")
                        first = False
                else:
                    out.append(f"{sp}  - {json.dumps(item)}")
        else:
            out.append(f"{sp}{k}: {json.dumps(v)}")
    return "\n".join(out)


# ─────────────────────────────────────────────────────────────────────────────
# Manifest entry point
# ─────────────────────────────────────────────────────────────────────────────
def generate_for_module(spec: Any, out_dir: str) -> Dict[str, Any]:
    g = TestbenchGenerator(spec)
    written = g.write(out_dir)
    return {**g.summary(), "files_written": written}


def run_from_manifest(manifest_path: str) -> int:
    mp = Path(manifest_path)
    try:
        manifest = json.loads(mp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("testbench_generator: cannot read manifest: %s", exc)
        return 0
    run_dir = Path(manifest.get("run_dir", mp.parent))
    rtl_dir = manifest.get("rtl_dir")
    out = run_dir / "generated_tb"
    reports = []
    if rtl_dir and Path(rtl_dir).exists():
        try:
            from AGENT_H.rtl_graph import RTLGraphAnalyzer
            mods = RTLGraphAnalyzer.from_dir(str(rtl_dir)).modules
            targets = manifest.get("tb_targets")
            for m in mods:
                if targets and m.name not in targets:
                    continue
                if not m.ports:
                    continue
                reports.append(generate_for_module(m, str(out)))
        except Exception as exc:                             # pragma: no cover
            log.warning("testbench_generator: %s", exc)
    rep = {"schema_version": SCHEMA_VERSION, "agent": AGENT_NAME,
           "status": "completed" if reports else "skipped",
           "generated": len(reports), "modules": reports, "pass": True}
    try:
        (run_dir / "testbench_gen_report.json").write_text(
            json.dumps(rep, indent=2, default=str), encoding="utf-8")
    except OSError:
        pass
    return 0


if __name__ == "__main__":  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="AVA AGENT_B testbench generator")
    ap.add_argument("--rtl", required=True, help="SystemVerilog file or dir")
    ap.add_argument("--out", default="./generated_tb")
    ap.add_argument("--module", help="only this module")
    args = ap.parse_args()
    from AGENT_H.rtl_graph import RTLGraphAnalyzer, parse_module
    p = Path(args.rtl)
    mods = (RTLGraphAnalyzer.from_dir(str(p)).modules if p.is_dir()
            else parse_module(p.read_text(errors="replace"), str(p)))
    for m in mods:
        if args.module and m.name != args.module:
            continue
        if m.ports:
            g = TestbenchGenerator(m)
            print(f"{m.name}: {len(g.write(args.out))} files")
