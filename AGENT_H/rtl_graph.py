"""
AGENT_H.rtl_graph — RTL Graph Construction & Structural Analysis (T77)
======================================================================

Turns real SystemVerilog into graphs the rest of the platform can reason over:
the foundation for GNN-style analysis, module similarity, clone detection and
automatic testbench generation.

Validated against **lowRISC Ibex** RTL (`corpus/ibex_rtl/`), not toy examples.

Scope — stated plainly
----------------------
This is a **structural parser**, not a full IEEE-1800 elaborator. It does not
evaluate generate blocks, resolve macros, or elaborate parameters. It extracts
what is reliably recoverable from source text: module boundaries, parameters,
ports, internal signals, continuous assignments, procedural blocks, submodule
instances, FSM state/transition structure and assertion sites. That is enough
for graph construction, similarity, and testbench scaffolding — and it is
honest about the cases it skips (reported in `parse_warnings`).

Graphs produced
---------------
- **Dataflow graph** — nodes are signals; an edge `u -> v` means `u` appears in
  the right-hand side of an assignment to `v`. Combinational vs sequential edges
  are distinguished (`always_ff` writes are marked `seq`), so combinational
  loops can be detected without false positives from registered feedback.
- **Control-flow graph** — nodes are procedural blocks and their `if`/`case`
  branches, capturing nesting depth and branch counts.
- **Module hierarchy graph** — instance edges between modules.
- **FSM graph** — extracted states and transitions (see below).

FSM extraction
--------------
Recognises the standard two-process idiom (`*_cs`/`*_ns`, `*_q`/`*_d`,
`state`/`next_state`): finds the `case` on the current-state variable, takes
the case labels as states, and every `next <= LABEL` / `next = LABEL` inside a
branch as a transition. The result plugs directly into
`rtl_basics_verifier`'s `fsm_def` format, so an extracted FSM can be checked
for unreachable states and deadlocks with no manual modelling.

Embeddings, similarity, clones
------------------------------
`embed()` produces a deterministic structural feature vector (port/signal/assign
counts, fan-in/out statistics, dataflow depth, operator histogram, control
nesting). `similarity()` is cosine over normalised embeddings; `find_clones()`
flags module pairs above a threshold. These are **unsupervised** — no training
data required — which is why they work today on Ibex as-is.

A learned GNN would sit on top of these graphs; that requires labelled data
(see `docs/DATA_AND_HARDWARE_REQUIREMENTS.md`). This module deliberately ships
the graph layer and honest structural metrics rather than an untrained network.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

log = logging.getLogger("AGENT_H.rtl_graph")

SCHEMA_VERSION = "2.1.0"
AGENT_NAME = "rtl_graph"

_SV_KEYWORDS = {
    "module", "endmodule", "input", "output", "inout", "logic", "wire", "reg",
    "parameter", "localparam", "begin", "end", "if", "else", "case", "casez",
    "casex", "endcase", "always", "always_ff", "always_comb", "always_latch",
    "assign", "posedge", "negedge", "or", "and", "not", "generate",
    "endgenerate", "for", "int", "bit", "signed", "unsigned", "default",
    "unique", "priority", "typedef", "enum", "struct", "import", "package",
    "endpackage", "function", "endfunction", "task", "endtask", "return",
    "initial", "assert", "property", "endproperty", "sequence", "wor",
    "genvar", "integer", "real", "time", "string", "const", "static",
    "automatic", "void", "this", "super", "null", "inside", "with",
}
_OPERATORS = ["&&", "||", "==", "!=", "<=", ">=", "<<", ">>", "^~", "~^",
              "&", "|", "^", "~", "+", "-", "*", "/", "%", "<", ">", "?"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    text = re.sub(r"//[^\n]*", " ", text)
    return text


def _srcs(expr: str, dst: str, lhs_indexed: bool) -> Set[str]:
    """Source identifiers of an assignment.

    When the left-hand side is a **bit or part select** (``x[i] = f(x[i-1])``),
    the apparent ``x -> x`` self-dependency is between *different bits* and is
    perfectly legal — the accumulate-in-a-for-loop idiom used throughout real
    RTL. Resolving that properly needs elaboration, which this parser does not
    do, so the self-edge is dropped rather than reported as a combinational
    loop. Dropping it can only cause a missed loop, never a false alarm.
    """
    srcs = _identifiers(expr)
    if lhs_indexed:
        srcs.discard(dst)
    return srcs


def _identifiers(expr: str) -> Set[str]:
    """Identifiers in an expression, minus keywords, numbers and sized literals."""
    expr = re.sub(r"\b\d+'[sS]?[bBhHdDoO][0-9a-fA-FxXzZ_]+", " ", expr)
    expr = re.sub(r"\b\d[\d_]*\b", " ", expr)
    out = set()
    for m in re.finditer(r"[A-Za-z_][A-Za-z0-9_$]*(?:::[A-Za-z_][A-Za-z0-9_$]*)?",
                         expr):
        tok = m.group(0)
        base = tok.split("::")[-1]
        if base in _SV_KEYWORDS or tok in _SV_KEYWORDS:
            continue
        out.add(base)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class Port:
    name: str
    direction: str
    width: str = "1"
    dtype: str = "logic"


@dataclass
class FSM:
    name: str
    state_reg: str
    next_reg: str
    states: List[str] = field(default_factory=list)
    transitions: List[Tuple[str, str]] = field(default_factory=list)
    reset_state: Optional[str] = None

    def to_fsm_def(self) -> Dict[str, Any]:
        """Emit the `fsm_def` record consumed by rtl_basics_verifier."""
        return {
            "event": "fsm_def", "name": self.name,
            "states": list(self.states),
            "reset": self.reset_state or (self.states[0] if self.states else ""),
            "transitions": [list(t) for t in self.transitions],
        }


@dataclass
class Module:
    name: str
    path: str = ""
    parameters: Dict[str, str] = field(default_factory=dict)
    ports: List[Port] = field(default_factory=list)
    signals: Set[str] = field(default_factory=set)
    assigns: List[Tuple[str, Set[str], str]] = field(default_factory=list)
    instances: List[Tuple[str, str]] = field(default_factory=list)
    always_blocks: List[Dict[str, Any]] = field(default_factory=list)
    fsms: List[FSM] = field(default_factory=list)
    assertions: List[str] = field(default_factory=list)
    parse_warnings: List[str] = field(default_factory=list)
    loc: int = 0

    # ── graphs ────────────────────────────────────────────────────────────
    def dataflow_graph(self) -> Dict[str, Set[str]]:
        g: Dict[str, Set[str]] = defaultdict(set)
        for dst, srcs, _kind in self.assigns:
            for s in srcs:
                g[s].add(dst)
            g.setdefault(dst, set())
        return dict(g)

    def comb_graph(self) -> Dict[str, Set[str]]:
        """Edges that can participate in a combinational loop.

        Excludes ``seq`` (registered — the flop breaks the loop) and ``ordered``
        (a blocking-assignment read that binds to a non-final version earlier in
        the same procedural block, which cannot close a loop).
        """
        g: Dict[str, Set[str]] = defaultdict(set)
        for dst, srcs, kind in self.assigns:
            if kind in ("seq", "ordered"):
                g.setdefault(dst, set())
                continue
            for s in srcs:
                g[s].add(dst)
            g.setdefault(dst, set())
        return dict(g)

    def inputs(self) -> List[str]:
        return [p.name for p in self.ports if p.direction == "input"]

    def outputs(self) -> List[str]:
        return [p.name for p in self.ports if p.direction == "output"]


# ─────────────────────────────────────────────────────────────────────────────
# Parser
# ─────────────────────────────────────────────────────────────────────────────
_MODULE_RE = re.compile(r"\bmodule\s+([A-Za-z_]\w*)", re.MULTILINE)
_PORT_RE = re.compile(
    r"\b(input|output|inout)\s+"
    r"(?:(wire|reg|logic|bit)\s+)?"
    r"(?:(signed|unsigned)\s+)?"
    r"((?:[A-Za-z_]\w*::)?[A-Za-z_]\w*(?:_[te])?\s+)?"
    r"(\[[^\]]*\]\s*)?"
    r"([A-Za-z_]\w*)")
_PARAM_RE = re.compile(r"\b(?:parameter|localparam)\s+"
                       r"(?:type\s+)?(?:\w+\s+)?(?:\[[^\]]*\]\s*)?"
                       r"([A-Za-z_]\w*)\s*=\s*([^,;)]+)")
_ASSIGN_RE = re.compile(r"\bassign\s+([A-Za-z_][\w.\[\]:]*)\s*=\s*([^;]+);")
# An instantiation is `type [#(params)] instance_name (`. The whitespace between
# the two identifiers is mandatory — without `\s+` the regex backtracks and
# splits control keywords (`for(` -> "fo" + "r") into bogus instances.
_INST_RE = re.compile(r"^[ \t]*([A-Za-z_]\w*)\s*(?:#\s*\([^;]*?\)\s*)?"
                      r"\s+([A-Za-z_]\w*)\s*\(", re.MULTILINE)
_NON_INSTANCE = {"if", "for", "case", "casez", "casex", "while", "repeat",
                 "assign", "always", "always_ff", "always_comb", "initial",
                 "function", "task", "return", "module", "import", "export",
                 "unique", "priority", "assert", "assume", "cover", "foreach",
                 "posedge", "negedge", "begin", "end", "else", "do"}
# Signal declarations. The width bracket may abut the type with no space
# (`logic[3:0] state, nstate;` in VeeR), so the whitespace after the type
# keyword must be optional when a bracket follows — otherwise those signals are
# never recorded and any FSM built on them is invisible.
_SIGDECL_RE = re.compile(
    r"^\s*(?:logic|wire|reg|bit)\b\s*(?:signed\s+|unsigned\s+)?"
    r"(?:\[[^\]]*\]\s*)?([A-Za-z_][\w,\s]*?)\s*;", re.MULTILINE)
# Assertion sites. Real cores use several idioms — backtick macros (Ibex,
# OpenTitan), plain `assert property` / `assume property` / `cover property`
# (CVA6, cv32e40p) and immediate `assert(`. Matching only the macro form
# reported 0 assertions for CVA6, which actually has 632.
_ASSERT_RE = re.compile(
    r"`(?:ASSERT\w*|ASSUME\w*|COVER\w*|DV_FCOV\w*)\s*\(\s*([A-Za-z_]\w*)"
    r"|(?:^|\W)(?:(?P<lbl>[A-Za-z_]\w*)\s*:\s*)?"
    r"(?P<kind>assert|assume|cover|restrict)\s+(?:property|final)?\s*\("
    r"|(?:^|\W)(?P<imm>assert)\s*\(")


def parse_module(text: str, path: str = "") -> List[Module]:
    """Parse every module in a SystemVerilog source file."""
    clean = strip_comments(text)
    mods: List[Module] = []
    starts = [(m.start(), m.group(1)) for m in _MODULE_RE.finditer(clean)]
    if not starts:
        return mods
    for i, (pos, name) in enumerate(starts):
        end = clean.find("endmodule", pos)
        end = end if end != -1 else (starts[i + 1][0] if i + 1 < len(starts)
                                     else len(clean))
        body = clean[pos:end]
        mod = Module(name=name, path=path, loc=body.count("\n") + 1)

        # header = up to the first ';' after the port list
        hdr_end = body.find(");")
        header = body[:hdr_end + 2] if hdr_end != -1 else body[:2000]
        for pm in _PARAM_RE.finditer(header):
            mod.parameters[pm.group(1)] = pm.group(2).strip()
        seen: Set[str] = set()
        for m in _PORT_RE.finditer(header):
            direction, _kw, _sign, dtype, width, pname = m.groups()
            if pname in seen or pname in _SV_KEYWORDS:
                continue
            seen.add(pname)
            mod.ports.append(Port(pname, direction,
                                  (width or "1").strip(),
                                  (dtype or "logic").strip()))
        rest = body[hdr_end + 2:] if hdr_end != -1 else body

        # internal signal declarations
        for sm in _SIGDECL_RE.finditer(rest):
            for nm in sm.group(1).split(","):
                nm = nm.strip()
                if nm and re.fullmatch(r"[A-Za-z_]\w*", nm):
                    mod.signals.add(nm)

        # continuous assignments
        for am in _ASSIGN_RE.finditer(rest):
            lhs = am.group(1)
            dst = lhs.split("[")[0].split(".")[0]
            mod.assigns.append((dst, _srcs(am.group(2), dst, "[" in lhs),
                                "comb"))

        # procedural blocks
        mod.always_blocks = _parse_always(rest, mod)

        # submodule instances (filter out keywords / known non-instances)
        for im in _INST_RE.finditer(rest):
            mtype, iname = im.group(1), im.group(2)
            if mtype in _SV_KEYWORDS or iname in _SV_KEYWORDS:
                continue
            if mtype in _NON_INSTANCE or iname in _NON_INSTANCE:
                continue
            mod.instances.append((mtype, iname))

        # assertion sites (named where the source names them)
        for asm in _ASSERT_RE.finditer(rest):
            name = (asm.group(1) or asm.group("lbl")
                    or asm.group("kind") or asm.group("imm"))
            if name:
                mod.assertions.append(name)

        # FSMs
        mod.fsms = extract_fsms(rest, mod)

        if "generate" in rest:
            mod.parse_warnings.append(
                "generate blocks are not elaborated; conditional instances may "
                "be missing")
        if "`ifdef" in rest or "`ifndef" in rest:
            mod.parse_warnings.append(
                "preprocessor conditionals not evaluated; both arms parsed")
        mods.append(mod)
    return mods


def _parse_always(text: str, mod: Module) -> List[Dict[str, Any]]:
    """Extract procedural blocks and record their writes as dataflow edges."""
    blocks: List[Dict[str, Any]] = []
    for m in re.finditer(r"\balways(_ff|_comb|_latch)?\b", text):
        kind = (m.group(1) or "").lstrip("_") or "generic"
        i = m.end()
        # optional sensitivity list @( ... ) — parsed with balanced parens so it
        # does not swallow the `begin` that may sit on the same line.
        sens = ""
        j = i
        while j < len(text) and text[j] in " \t\n\r":
            j += 1
        if j < len(text) and text[j] == "@":
            k = text.find("(", j)
            if k != -1:
                depth, e = 0, k
                while e < len(text):
                    if text[e] == "(":
                        depth += 1
                    elif text[e] == ")":
                        depth -= 1
                        if depth == 0:
                            break
                    e += 1
                sens = text[j:e + 1]
                i = e + 1
            else:
                i = j + 1
        # block body: balanced begin/end, or a single statement up to ';'
        while i < len(text) and text[i] in " \t\n\r":
            i += 1
        start = i
        if re.match(r"begin\b", text[i:]):
            i += 5
            start = i
            depth = 1
            while i < len(text) and depth > 0:
                if re.match(r"begin\b", text[i:]):
                    depth += 1
                    i += 5
                    continue
                if re.match(r"end\b", text[i:]) and not re.match(r"endcase\b",
                                                                 text[i:]):
                    depth -= 1
                    i += 3
                    continue
                i += 1
            body = text[start:i]
        else:
            j2 = text.find(";", i)
            body = text[i:(j2 + 1) if j2 != -1 else len(text)]
        seq = kind == "ff"
        # Blocking assignments execute in order, so a reference to `x` on the
        # right-hand side of a *later* statement binds to the value defined
        # earlier in the same block — an ordered combinational cascade, not
        # feedback. (Real example, ibex_alu: `rev_result = operand_a_i;` then
        # `rev_result = f(rev_result);`.) Tracking which names have already been
        # defined in this block is the SSA insight that keeps that idiom from
        # being mis-reported as a combinational loop.
        stmts: List[Tuple[str, str, Set[str], bool]] = []
        for am in re.finditer(r"([A-Za-z_][\w.\[\]:]*)\s*(<=|\|=|&=|\^=|=)\s*"
                              r"([^;]+);", body):
            op = am.group(2)
            if op == "=" and re.search(r"[=!<>]=", am.group(0)[:am.start(2)]):
                continue
            lhs = am.group(1)
            dst = lhs.split("[")[0].split(".")[0]
            if dst in _SV_KEYWORDS:
                continue
            srcs = _srcs(am.group(3), dst, "[" in lhs)
            if op in ("|=", "&=", "^="):
                srcs.discard(dst)
            stmts.append((dst, op, srcs, "[" in lhs))

        # Versioned (SSA) resolution of the block. Each blocking assignment
        # creates a new version of its target; reads bind to the version
        # current at that point (version 0 = the value on block entry). The
        # block's observable output for a signal is its *final* version, so we
        # resolve each output down to the set of **entry** values it depends
        # on. A signal whose output depends on its own entry value is genuine
        # combinational feedback; the ordered cascades that pervade real RTL
        # (`x = a; x = f(x);`) resolve to `a` and correctly show no loop.
        ver: Dict[str, int] = {}
        deps: Dict[Tuple[str, int], Set[Tuple[str, int]]] = {}
        for dst, op, srcs, _ix in stmts:
            if op == "<=":
                mod.assigns.append((dst, srcs, "seq"))
                continue
            bound = {(s, ver.get(s, 0)) for s in srcs}
            ver[dst] = ver.get(dst, 0) + 1
            deps[(dst, ver[dst])] = bound

        memo: Dict[Tuple[str, int], Set[str]] = {}

        def _resolve(node: Tuple[str, int], seen: Set[Tuple[str, int]]) -> Set[str]:
            name, v = node
            if v == 0:
                return {name}                      # entry value
            if node in memo:
                return memo[node]
            if node in seen:
                return set()
            seen = seen | {node}
            out: Set[str] = set()
            for d in deps.get(node, ()):  # noqa: B020
                out |= _resolve(d, seen)
            memo[node] = out
            return out

        for name, v in ver.items():
            if v > 0:
                mod.assigns.append((name, _resolve((name, v), set()),
                                    "seq" if seq else "comb"))
        blocks.append({
            "kind": kind,
            "sensitivity": sens.strip()[:120],
            "branches": len(re.findall(r"\bif\b", body)),
            "cases": len(re.findall(r"\bcase[zx]?\b", body)),
            "depth": _max_nesting(body),
            "loc": body.count("\n") + 1,
        })
    return blocks


def _max_nesting(body: str) -> int:
    depth = maxd = 0
    for tok in re.finditer(r"\b(begin|end)\b", body):
        if tok.group(1) == "begin":
            depth += 1
            maxd = max(maxd, depth)
        else:
            depth = max(0, depth - 1)
    return maxd


# ─────────────────────────────────────────────────────────────────────────────
# FSM extraction
# ─────────────────────────────────────────────────────────────────────────────
# Current/next state naming idioms differ per project: Ibex uses `*_cs`/`*_ns`,
# VeeR uses `state`/`nstate` and `state`/`next_state`, others use `*_q`/`*_d`.
# Covering only one family silently reported 0 FSMs for VeeR EH1.
_STATE_SUFFIX_PAIRS = [("_cs", "_ns"), ("_q", "_d"), ("_r", "_nxt"),
                       ("_reg", "_next"), ("_cur", "_next")]


def _next_name_candidates(s: str) -> List[str]:
    """Plausible next-state partner names for a current-state signal."""
    out: List[str] = []
    for cur_suf, nxt_suf in _STATE_SUFFIX_PAIRS:
        if s.endswith(cur_suf):
            out.append(s[: -len(cur_suf)] + nxt_suf)
    out += [s + "_ns", s + "_nxt", s + "_next", s + "_d",
            "n" + s, "next_" + s, "nxt_" + s]
    return out


def _case_arms(body: str) -> List[Tuple[str, str]]:
    """Split a case body into (label, arm_body) — a sequential scanner.

    Two real-RTL hazards this must survive:
    - **nested begin/end**: a non-greedy `begin … end` stops at the first inner
      `end` (which closes an `if`) and truncates the arm, losing transitions.
    - **ternary colons**: `ns = c ? A : B;` contains a `:` that is *not* a new
      case label. A regex that keys on every `id :` mis-splits it.

    Scanning forward — consume `LABEL :`, then either a balanced `begin…end`
    block or a single statement up to `;`, then continue *after* it — avoids
    both, because we never look for a label inside a statement we have already
    consumed.
    """
    arms: List[Tuple[str, str]] = []
    i, n = 0, len(body)
    label_re = re.compile(r"\s*([A-Za-z_]\w*|\d+'[bBhHdDoO][0-9a-fA-FxXzZ_]+|"
                          r"\d+)\s*:")
    while i < n:
        m = label_re.match(body, i)
        if not m:
            # not at an arm boundary; skip to the next ';' or 'begin'/'end'
            nxt_semi = body.find(";", i)
            if nxt_semi == -1:
                break
            i = nxt_semi + 1
            continue
        label = m.group(1)
        i = m.end()
        while i < n and body[i] in " \t\r\n":
            i += 1
        if re.match(r"begin\b", body[i:]):
            i += 5
            start = i
            depth = 1
            while i < n and depth > 0:
                if re.match(r"begin\b", body[i:]):
                    depth += 1
                    i += 5
                    continue
                if re.match(r"end\b", body[i:]) and not re.match(r"endcase\b",
                                                                 body[i:]):
                    depth -= 1
                    i += 3
                    continue
                i += 1
            arms.append((label, body[start:i]))
        else:
            j = body.find(";", i)
            end = j + 1 if j != -1 else n
            arms.append((label, body[i:end]))
            i = end
    return arms


def extract_fsms(text: str, mod: Module) -> List[FSM]:
    """Find two-process FSMs and recover states + transitions."""
    fsms: List[FSM] = []
    candidates: List[Tuple[str, str]] = []
    idents = set(mod.signals)
    for s in idents:
        for nxt in _next_name_candidates(s):
            if nxt != s and nxt in idents:
                candidates.append((s, nxt))
    # also catch declarations via a user-defined enum type (e.g. `ctrl_fsm_e a, b;`)
    for m in re.finditer(r"^\s*([A-Za-z_]\w*_e)\s+([A-Za-z_]\w*)\s*,\s*"
                         r"([A-Za-z_]\w*)\s*;", text, re.MULTILINE):
        candidates.append((m.group(2), m.group(3)))

    for cur, nxt in dict.fromkeys(candidates):
        cm = re.search(r"\b(?:unique\s+|priority\s+)?case[zx]?\s*\(\s*"
                       + re.escape(cur) + r"\s*\)", text)
        if not cm:
            continue
        endc = text.find("endcase", cm.end())
        if endc == -1:
            continue
        body = text[cm.end():endc]
        arms = _case_arms(body)
        # Pass 1: the case labels are the state set.
        states: List[str] = []
        for label, _arm in arms:
            if label in _SV_KEYWORDS or label == "default":
                continue
            if label not in states:
                states.append(label)
        state_set = set(states)
        # Pass 2: a transition is a next-state assignment whose right-hand side
        # names a known state. Scanning the RHS for *known-state* identifiers
        # (rather than taking the first token) handles direct assignments
        # (`ns = A;`) and ternaries alike (`ns = c ? A : B;` → two transitions,
        # not the bogus `ns -> c`).
        transitions: List[Tuple[str, str]] = []
        for label, arm_body in arms:
            if label in _SV_KEYWORDS or label == "default":
                continue
            for am in re.finditer(re.escape(nxt) + r"\s*(?:<=|=)\s*([^;]+);",
                                  arm_body):
                for idm in re.finditer(r"[A-Za-z_]\w*", am.group(1)):
                    tgt = idm.group(0)
                    if tgt in state_set and (label, tgt) not in transitions:
                        transitions.append((label, tgt))
        if not states:
            continue
        # reset state: what the sequential block loads on reset
        reset_state = None
        rm = re.search(r"if\s*\(\s*!\s*\w*rst\w*\s*\)\s*begin(.*?)\bend\b",
                       text, re.DOTALL)
        if rm:
            rv = re.search(re.escape(cur) + r"\s*<=\s*([A-Za-z_]\w*)",
                           rm.group(1))
            if rv:
                reset_state = rv.group(1)
        for s in [t for _, t in transitions]:
            if s not in states:
                states.append(s)
        fsms.append(FSM(name=f"{mod.name}.{cur}", state_reg=cur, next_reg=nxt,
                        states=states, transitions=transitions,
                        reset_state=reset_state))
    return fsms


# ─────────────────────────────────────────────────────────────────────────────
# Graph metrics, embeddings, similarity
# ─────────────────────────────────────────────────────────────────────────────
def find_comb_loops(graph: Dict[str, Set[str]]) -> List[List[str]]:
    """Cycles in the combinational dataflow graph (real design errors)."""
    colour: Dict[str, int] = {}
    out: List[List[str]] = []
    parent: Dict[str, str] = {}

    def walk(u: str) -> None:
        colour[u] = 1
        for v in graph.get(u, ()):  # noqa: B020
            if colour.get(v, 0) == 0:
                parent[v] = u
                walk(v)
            elif colour.get(v) == 1:
                cyc = [v, u]
                x = u
                while x in parent and parent[x] != v and len(cyc) < 64:
                    x = parent[x]
                    cyc.append(x)
                out.append(list(reversed(cyc)))
        colour[u] = 2

    import sys
    old = sys.getrecursionlimit()
    sys.setrecursionlimit(max(10000, old))
    try:
        for n in list(graph):
            if colour.get(n, 0) == 0:
                walk(n)
    except RecursionError:               # pathological depth: report nothing
        pass
    finally:
        sys.setrecursionlimit(old)
    return out[:20]


def graph_depth(graph: Dict[str, Set[str]], sources: Sequence[str]) -> int:
    """Longest shortest-path from any source (logic-depth proxy)."""
    best = 0
    for s in sources:
        seen = {s}
        frontier = [s]
        d = 0
        while frontier and d < 200:
            nxt = []
            for u in frontier:
                for v in graph.get(u, ()):  # noqa: B020
                    if v not in seen:
                        seen.add(v)
                        nxt.append(v)
            if nxt:
                d += 1
            frontier = nxt
        best = max(best, d)
    return best


EMBED_KEYS = [
    "n_ports", "n_inputs", "n_outputs", "n_params", "n_signals", "n_assigns",
    "n_seq_assigns", "n_always", "n_instances", "n_fsms", "n_assertions",
    "mean_fanin", "max_fanin", "mean_fanout", "max_fanout", "depth",
    "max_nesting", "n_branches", "n_cases", "loc",
]


def embed(mod: Module) -> Dict[str, float]:
    g = mod.dataflow_graph()
    fanout = {n: len(succ) for n, succ in g.items()}
    fanin: Dict[str, int] = defaultdict(int)
    for _n, succ in g.items():
        for s in succ:
            fanin[s] += 1
    fi = list(fanin.values()) or [0]
    fo = list(fanout.values()) or [0]
    seq = sum(1 for _d, _s, k in mod.assigns if k == "seq")
    return {
        "n_ports": float(len(mod.ports)),
        "n_inputs": float(len(mod.inputs())),
        "n_outputs": float(len(mod.outputs())),
        "n_params": float(len(mod.parameters)),
        "n_signals": float(len(mod.signals)),
        "n_assigns": float(len(mod.assigns)),
        "n_seq_assigns": float(seq),
        "n_always": float(len(mod.always_blocks)),
        "n_instances": float(len(mod.instances)),
        "n_fsms": float(len(mod.fsms)),
        "n_assertions": float(len(mod.assertions)),
        "mean_fanin": sum(fi) / len(fi),
        "max_fanin": float(max(fi)),
        "mean_fanout": sum(fo) / len(fo),
        "max_fanout": float(max(fo)),
        "depth": float(graph_depth(mod.comb_graph(), mod.inputs()[:24])),
        "max_nesting": float(max([b["depth"] for b in mod.always_blocks] or [0])),
        "n_branches": float(sum(b["branches"] for b in mod.always_blocks)),
        "n_cases": float(sum(b["cases"] for b in mod.always_blocks)),
        "loc": float(mod.loc),
    }


def _vec(e: Dict[str, float]) -> List[float]:
    return [float(e.get(k, 0.0)) for k in EMBED_KEYS]


def similarity(a: Dict[str, float], b: Dict[str, float]) -> float:
    """Cosine over log-scaled feature vectors (scale-robust).

    A module the parser recovered nothing from yields an all-zero vector. Two
    such vectors must **not** score as identical: absence of evidence is not
    evidence of sameness. (Scoring them 1.0 produced 1734 bogus "clones" on
    CVA6 — every unparsed stub matched every other one.)
    """
    # `loc` (line count) alone carries no structural signal: an empty stub still
    # has loc>=1. If either side has no *structural* feature (everything except
    # loc is zero) there is nothing meaningful to compare, so return 0.
    struct_keys = [k for k in EMBED_KEYS if k != "loc"]
    if sum(a.get(k, 0.0) for k in struct_keys) == 0 or \
       sum(b.get(k, 0.0) for k in struct_keys) == 0:
        return 0.0
    va = [math.log1p(max(0.0, x)) for x in _vec(a)]
    vb = [math.log1p(max(0.0, x)) for x in _vec(b)]
    na = math.sqrt(sum(x * x for x in va))
    nb = math.sqrt(sum(y * y for y in vb))
    if na == 0 or nb == 0:
        return 0.0
    dot = sum(x * y for x, y in zip(va, vb))
    return dot / (na * nb)


def _structural_mass(m: Module) -> int:
    """How much structure we actually recovered — the basis for whether a
    similarity claim about this module is meaningful at all."""
    return (len(m.assigns) + len(m.ports) + len(m.instances)
            + len(m.always_blocks))


def find_clones(modules: Sequence[Module], threshold: float = 0.98,
                min_mass: int = 6) -> List[Dict[str, Any]]:
    """Structurally near-identical module pairs (clone / copy-paste candidates).

    Modules below ``min_mass`` are skipped: with almost no recovered structure
    any two of them look alike, which floods the result with noise rather than
    finding real copy-paste.
    """
    embs = [(m, embed(m)) for m in modules if _structural_mass(m) >= min_mass]
    out = []
    for i in range(len(embs)):
        for j in range(i + 1, len(embs)):
            s = similarity(embs[i][1], embs[j][1])
            if s >= threshold:
                out.append({"a": embs[i][0].name, "b": embs[j][0].name,
                            "similarity": round(s, 6),
                            "verdict": "clone_candidate"})
    out.sort(key=lambda r: -r["similarity"])
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Directory analysis
# ─────────────────────────────────────────────────────────────────────────────
class RTLGraphAnalyzer:
    def __init__(self, modules: Optional[Sequence[Module]] = None):
        self.modules: List[Module] = list(modules or [])

    @classmethod
    def from_dir(cls, path: str, pattern: str = "*.sv") -> "RTLGraphAnalyzer":
        mods: List[Module] = []
        root = Path(path)
        files = sorted(list(root.rglob(pattern)) + list(root.rglob("*.v")))
        for f in files:
            try:
                mods.extend(parse_module(f.read_text(encoding="utf-8",
                                                     errors="replace"), str(f)))
            except OSError as exc:
                log.warning("rtl_graph: cannot read %s: %s", f, exc)
        return cls(mods)

    def hierarchy(self) -> Dict[str, Set[str]]:
        names = {m.name for m in self.modules}
        g: Dict[str, Set[str]] = {m.name: set() for m in self.modules}
        for m in self.modules:
            for mtype, _inst in m.instances:
                if mtype in names:
                    g[m.name].add(mtype)
        return g

    def run(self) -> Dict[str, Any]:
        started = _now()
        mods = []
        all_fsms = []
        loops = []
        for m in self.modules:
            e = embed(m)
            cl = find_comb_loops(m.comb_graph())
            if cl:
                loops.append({"module": m.name, "cycles": cl[:3]})
            for f in m.fsms:
                all_fsms.append(f.to_fsm_def())
            mods.append({
                "name": m.name, "path": m.path, "loc": m.loc,
                "ports": len(m.ports), "inputs": len(m.inputs()),
                "outputs": len(m.outputs()),
                "parameters": sorted(m.parameters),
                "signals": len(m.signals), "assigns": len(m.assigns),
                "instances": [i[0] for i in m.instances],
                "fsms": [f.name for f in m.fsms],
                "assertions": m.assertions,
                "embedding": e,
                "parse_warnings": m.parse_warnings,
            })
        clones = find_clones(self.modules)
        return {
            "schema_version": SCHEMA_VERSION,
            "agent": AGENT_NAME,
            "started_at": started,
            "finished_at": _now(),
            "metrics": {
                "modules": len(self.modules),
                "total_loc": sum(m.loc for m in self.modules),
                "fsms_found": len(all_fsms),
                "clone_pairs": len(clones),
                "modules_with_comb_loops": len(loops),
                "total_assertions": sum(len(m.assertions) for m in self.modules),
            },
            "modules": mods,
            "fsm_defs": all_fsms,
            "clones": clones,
            "combinational_loops": loops,
            "hierarchy": {k: sorted(v) for k, v in self.hierarchy().items()},
            "pass": not loops,
        }


def run_from_manifest(manifest_path: str) -> int:
    mp = Path(manifest_path)
    try:
        manifest = json.loads(mp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("rtl_graph: cannot read manifest: %s", exc)
        return 0
    run_dir = Path(manifest.get("run_dir", mp.parent))
    src = manifest.get("rtl_dir")
    if not src or not Path(src).exists():
        rep = {"schema_version": SCHEMA_VERSION, "agent": AGENT_NAME,
               "status": "skipped", "reason": "no rtl_dir", "pass": True}
    else:
        rep = RTLGraphAnalyzer.from_dir(src).run()
        rep["status"] = "completed"
    try:
        (run_dir / "rtl_graph_report.json").write_text(
            json.dumps(rep, indent=2, default=str), encoding="utf-8")
    except OSError as exc:
        log.warning("rtl_graph: cannot write report: %s", exc)
    return 0 if rep.get("pass", True) else 1


if __name__ == "__main__":  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="AVA RTL graph analyzer")
    ap.add_argument("--rtl-dir", required=True)
    ap.add_argument("--json", help="write the report here")
    args = ap.parse_args()
    rep = RTLGraphAnalyzer.from_dir(args.rtl_dir).run()
    if args.json:
        Path(args.json).write_text(json.dumps(rep, indent=2, default=str))
    print(json.dumps(rep["metrics"], indent=2))
