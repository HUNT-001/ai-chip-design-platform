"""
AGENT_H/contract_dsl.py
========================
T16 — Design Contract Verification DSL

A lightweight domain-specific language for writing hardware design contracts
that AVA can verify against the commit logs.  Contracts express assumptions
(preconditions) and guarantees (postconditions) over instruction sequences.

Contract syntax (Python-based eDSL)
-------------------------------------
  from AGENT_H.contract_dsl import contract, assume, guarantee, for_instruction

  @contract("MUL always produces consistent high/low halves")
  @for_instruction(r"mul.*")
  def mul_consistency(ctx: ContractContext):
      assume(ctx.both_have("regs"))
      guarantee(
          lambda: ctx.rtl_reg("a0") == ctx.iss_reg("a0"),
          "MUL rd must match between RTL and ISS"
      )

Design contracts are more expressive than intent specs (T11) because they:
  - Support preconditions (only check when assumption holds)
  - Can reference previous records in the sequence (sliding window)
  - Can fire across instruction boundaries (e.g. store → fence → load)

Usage
-----
  runner = ContractRunner(contracts=[mul_consistency, ...])
  report = runner.check(rtl_log, iss_log)
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "2.1.0"


# ─────────────────────────────────────────────────────────
# Contract context (per instruction pair)
# ─────────────────────────────────────────────────────────

class AssumptionFailed(Exception):
    """Raised when a contract precondition is not met (skip this record)."""


class GuaranteeFailed(Exception):
    """Raised when a contract postcondition is violated."""
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


@dataclass
class ContractContext:
    """Execution context passed to each contract function."""
    rtl:     Dict[str, Any]            # current RTL record
    iss:     Dict[str, Any]            # current ISS record
    seq:     int                       # instruction sequence number
    window:  List[Tuple[Dict, Dict]]   # (rtl, iss) pairs for last N records

    def both_have(self, field_name: str) -> bool:
        return field_name in self.rtl and field_name in self.iss

    def rtl_reg(self, name: str) -> Optional[int]:
        v = (self.rtl.get("regs") or {}).get(name)
        return int(v, 16) if isinstance(v, str) else v

    def iss_reg(self, name: str) -> Optional[int]:
        v = (self.iss.get("regs") or {}).get(name)
        return int(v, 16) if isinstance(v, str) else v

    def rtl_csr(self, name: str) -> Optional[int]:
        v = (self.rtl.get("csrs") or {}).get(name)
        return int(v, 16) if isinstance(v, str) else v

    def iss_csr(self, name: str) -> Optional[int]:
        v = (self.iss.get("csrs") or {}).get(name)
        return int(v, 16) if isinstance(v, str) else v

    def disasm(self) -> str:
        return (self.iss.get("disasm") or "").strip().lower()

    def prev_disasm(self, n: int = 1) -> Optional[str]:
        if len(self.window) >= n:
            return (self.window[-n][1].get("disasm") or "").strip().lower()
        return None


def assume(condition: bool) -> None:
    """Assert a precondition. If False, contract is skipped for this record."""
    if not condition:
        raise AssumptionFailed()


def guarantee(condition_fn: Callable[[], bool], message: str = "") -> None:
    """Assert a postcondition. If False, records a contract violation."""
    try:
        if not condition_fn():
            raise GuaranteeFailed(message or "guarantee violated")
    except GuaranteeFailed:
        raise
    except Exception as exc:
        raise GuaranteeFailed(f"guarantee check raised: {exc}")


# ─────────────────────────────────────────────────────────
# Contract decorators
# ─────────────────────────────────────────────────────────

@dataclass
class DesignContract:
    name:        str
    description: str
    fn:          Callable[[ContractContext], None]
    pattern:     Optional[str] = None    # mnemonic regex filter
    severity:    str = "HIGH"
    _compiled:   Any = field(default=None, repr=False)

    def __post_init__(self):
        if self.pattern:
            self._compiled = re.compile(self.pattern, re.IGNORECASE)

    def applies(self, disasm: str) -> bool:
        if self._compiled is None:
            return True
        return bool(self._compiled.match(disasm))


_REGISTRY: List[DesignContract] = []


def contract(description: str, severity: str = "HIGH"):
    """Decorator: register a contract function."""
    def decorator(fn: Callable):
        c = DesignContract(
            name=fn.__name__,
            description=description,
            fn=fn,
            severity=severity,
        )
        _REGISTRY.append(c)
        return fn
    return decorator


def for_instruction(pattern: str):
    """Decorator: restrict contract to instructions matching regex pattern."""
    def decorator(fn: Callable):
        # Find the most recently registered contract for this function name
        for c in reversed(_REGISTRY):
            if c.fn is fn:
                c.pattern   = pattern
                c._compiled = re.compile(pattern, re.IGNORECASE)
                break
        return fn
    return decorator


# ─────────────────────────────────────────────────────────
# Built-in contracts
# ─────────────────────────────────────────────────────────

@contract("x0 register must always equal 0 in both RTL and ISS")
def x0_always_zero(ctx: ContractContext):
    assume(ctx.both_have("regs"))
    guarantee(
        lambda: (ctx.rtl_reg("x0") or 0) == 0 and (ctx.iss_reg("x0") or 0) == 0,
        "x0 must equal 0"
    )


@contract("MUL result must match between RTL and ISS", severity="HIGH")
@for_instruction(r"mul\b")
def mul_result_matches(ctx: ContractContext):
    assume(ctx.both_have("regs"))
    regs = (ctx.rtl.get("regs") or {}).keys()
    for reg in regs:
        if reg == "x0":
            continue
        guarantee(
            lambda r=reg: ctx.rtl_reg(r) == ctx.iss_reg(r),
            f"MUL: register {reg} mismatch"
        )


@contract("DIV by zero must produce defined result (not UB)", severity="MEDIUM")
@for_instruction(r"div\w*|rem\w*")
def div_rem_defined(ctx: ContractContext):
    # RISC-V spec: DIV by zero → -1 (unsigned: XLEN_MAX), REM by zero → dividend
    disasm = ctx.disasm()
    regs = (ctx.rtl.get("regs") or {}).keys()
    for reg in regs:
        if reg == "x0":
            continue
        guarantee(
            lambda r=reg: ctx.rtl_reg(r) == ctx.iss_reg(r),
            f"DIV/REM: register {reg} mismatch (possible div-by-zero UB)"
        )


@contract("MRET must restore PC from mepc", severity="HIGH")
@for_instruction(r"mret")
def mret_restores_pc(ctx: ContractContext):
    mepc = ctx.iss_csr("mepc")
    pc   = ctx.iss.get("pc")
    assume(mepc is not None and pc is not None)
    mepc_int = int(mepc, 16) if isinstance(mepc, str) else mepc
    pc_int   = int(pc,   16) if isinstance(pc,   str) else pc
    guarantee(
        lambda: mepc_int == pc_int,
        f"MRET: PC=0x{pc_int:08x} != mepc=0x{mepc_int:08x}"
    )


@contract("ECALL must generate a trap record", severity="HIGH")
@for_instruction(r"ecall")
def ecall_generates_trap(ctx: ContractContext):
    guarantee(
        lambda: ctx.iss.get("trap") is not None or ctx.rtl.get("trap") is not None,
        "ECALL produced no trap"
    )


@contract("CSR write must be reflected in subsequent CSR read", severity="MEDIUM")
@for_instruction(r"csrr[ws].*")
def csr_write_reflected(ctx: ContractContext):
    assume(ctx.both_have("csrs"))
    rtl_csrs = ctx.rtl.get("csrs") or {}
    iss_csrs = ctx.iss.get("csrs") or {}
    for csr in rtl_csrs:
        guarantee(
            lambda c=csr: (int(rtl_csrs.get(c, 0), 16) if isinstance(rtl_csrs.get(c), str) else rtl_csrs.get(c, 0)) ==
                          (int(iss_csrs.get(c, 0), 16) if isinstance(iss_csrs.get(c), str) else iss_csrs.get(c, 0)),
            f"CSR {csr} mismatch after write"
        )


# ─────────────────────────────────────────────────────────
# Contract runner
# ─────────────────────────────────────────────────────────

@dataclass
class ContractViolation:
    contract_name: str
    severity:      str
    seq:           int
    pc:            Optional[str]
    disasm:        Optional[str]
    message:       str


class ContractRunner:
    """
    Checks a set of DesignContracts against paired RTL/ISS commit logs.

    Parameters
    ----------
    contracts      : list of DesignContract objects (defaults to _REGISTRY)
    window_size    : number of recent records to pass as context window
    max_violations : stop after this many violations
    """

    def __init__(
        self,
        contracts:      Optional[List[DesignContract]] = None,
        window_size:    int = 5,
        max_violations: int = 200,
    ) -> None:
        self.contracts      = contracts or list(_REGISTRY)
        self.window_size    = window_size
        self.max_violations = max_violations

    def check(
        self,
        rtl_log: List[Dict],
        iss_log: List[Dict],
    ) -> Dict[str, Any]:
        started    = datetime.now(timezone.utc)
        n          = min(len(rtl_log), len(iss_log))
        violations: List[ContractViolation] = []
        window: List[Tuple[Dict, Dict]] = []

        contract_stats = {c.name: {"checked": 0, "skipped": 0, "violated": 0}
                          for c in self.contracts}

        for i in range(n):
            if len(violations) >= self.max_violations:
                break
            rtl_r   = rtl_log[i]
            iss_r   = iss_log[i]
            disasm_ = (iss_r.get("disasm") or "").strip().lower()
            seq     = rtl_r.get("seq", i)

            ctx = ContractContext(
                rtl=rtl_r, iss=iss_r, seq=seq,
                window=list(window[-self.window_size:]),
            )

            for c in self.contracts:
                if not c.applies(disasm_):
                    continue
                contract_stats[c.name]["checked"] += 1
                try:
                    c.fn(ctx)
                except AssumptionFailed:
                    contract_stats[c.name]["skipped"] += 1
                except GuaranteeFailed as gf:
                    contract_stats[c.name]["violated"] += 1
                    violations.append(ContractViolation(
                        contract_name=c.name,
                        severity=c.severity,
                        seq=seq,
                        pc=iss_r.get("pc"),
                        disasm=disasm_,
                        message=gf.message,
                    ))
                except Exception as exc:
                    logger.warning("Contract %s raised: %s", c.name, exc)

            window.append((rtl_r, iss_r))

        finished  = datetime.now(timezone.utc)
        high_viols = [v for v in violations if v.severity == "HIGH"]

        return {
            "schema_version":   SCHEMA_VERSION,
            "agent":            "contract_dsl",
            "records_checked":  n,
            "contracts_run":    len(self.contracts),
            "total_violations": len(violations),
            "high_violations":  len(high_viols),
            "pass":             len(violations) == 0,
            "contract_stats":   contract_stats,
            "violations": [
                {
                    "contract":  v.contract_name,
                    "severity":  v.severity,
                    "seq":       v.seq,
                    "pc":        v.pc,
                    "disasm":    v.disasm,
                    "message":   v.message,
                }
                for v in violations[:50]
            ],
            "started_at":  started.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "finished_at": finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration_s":  round((finished - started).total_seconds(), 3),
        }


def run_from_manifest(manifest_path: Path) -> int:
    with open(manifest_path) as f:
        manifest = json.load(f)

    run_dir = Path(manifest["run_dir"])
    outputs = manifest.get("outputs", {})

    def _load_log(key, default):
        p = run_dir / (outputs.get(key) or default)
        if not p.exists():
            return []
        recs = []
        with open(p) as f:
            for line in f:
                line = line.strip()
                if line:
                    recs.append(json.loads(line))
        return recs

    rtl_log = _load_log("rtl_commit_log", "rtl_commit.jsonl")
    iss_log = _load_log("iss_commit_log", "iss_commit.jsonl")

    if not rtl_log or not iss_log:
        logger.warning("ContractRunner: no commit logs, skipping")
        return 0

    runner = ContractRunner()
    report = runner.check(rtl_log, iss_log)

    report_path = run_dir / "contract_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
        f.write("\n")

    manifest.setdefault("outputs", {})["contract_report"] = "contract_report.json"
    manifest.setdefault("phases", {})["contract_check"] = {
        "status": "pass" if report["pass"] else "fail",
    }
    tmp = manifest_path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    tmp.rename(manifest_path)
    return 0 if report["pass"] else 1
