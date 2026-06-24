"""
AGENT_H/explainer.py
====================
T15 — Explainability Layer

Produces structured, human-readable natural-language explanations for every
AVA bug report.  The explanation covers:

  1. What went wrong (symptom summary)
  2. Where it went wrong (module + PC)
  3. Why it likely went wrong (causal hypothesis from Agent D / root cause)
  4. How to reproduce it (minimal test from the minimizer)
  5. What to check next (recommended follow-up actions)

The output is designed for two audiences:
  - Hardware designers: technical detail on the failing path
  - Verification managers: plain-English summary for sign-off dashboards

Usage
-----
  from AGENT_H.explainer import BugExplainer

  explainer = BugExplainer(bug_report, root_cause, intent_report, minimizer_stats)
  doc = explainer.explain()
  print(doc["summary"])           # one-liner
  print(doc["technical_detail"])  # full prose
  print(doc["recommended_actions"])
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "2.1.0"


# ─────────────────────────────────────────────────────────
# Per-class explanation templates
# ─────────────────────────────────────────────────────────

_SYMPTOM_TEMPLATES: Dict[str, str] = {
    "REG_MISMATCH": (
        "The RTL implementation produced a different register value than the "
        "RISC-V ISS reference at instruction sequence {seq} (PC={pc}). "
        "The diverging register was {detail_register} — "
        "RTL computed {detail_rtl_val}, ISS expected {detail_iss_val}."
    ),
    "PC_MISMATCH": (
        "The RTL program counter diverged from the ISS reference at sequence {seq}. "
        "RTL PC={detail_rtl_val}, ISS PC={detail_iss_val}. "
        "This typically indicates a branch prediction, jump, or trap-return error."
    ),
    "MEM_MISMATCH": (
        "A memory operation produced different results in RTL vs ISS at sequence {seq} "
        "(PC={pc}). The RTL store or load to address {detail_address} yielded "
        "{detail_rtl_val}, but the ISS expected {detail_iss_val}."
    ),
    "CSR_MISMATCH": (
        "Control and Status Register {detail_register} diverged at sequence {seq} "
        "(PC={pc}). The RTL value was {detail_rtl_val} but the ISS held {detail_iss_val}. "
        "This may indicate a privilege-mode, trap-delegation, or CSR-write ordering issue."
    ),
    "TRAP_MISMATCH": (
        "A trap (exception or interrupt) was handled differently at sequence {seq} "
        "(PC={pc}). {detail_description} "
        "Trap handling divergence can lead to silent privilege escalation."
    ),
    "ORDERMISMATCH": (
        "A memory ordering violation was detected at sequence {seq} (PC={pc}). "
        "The RTL did not enforce the RVWMO ordering constraint implied by a FENCE or "
        "AMO instruction, causing a store-load reordering visible to the ISS."
    ),
    "EQUIV_FAIL": (
        "The synthesised netlist is not functionally equivalent to the RTL source. "
        "Agent L's equivalence checker found a counterexample at BMC depth {detail_depth}."
    ),
    "CDC_VIOLATION": (
        "A Clock Domain Crossing signal was detected without a proper synchroniser. "
        "The unsafe path is {detail_signal} from domain {detail_from} to {detail_to}."
    ),
    "INTENT_VIOLATION": (
        "An architectural intent invariant was violated at sequence {seq} (PC={pc}). "
        "Spec '{detail_spec}': {detail_description}."
    ),
}

_ROOT_CAUSE_TEMPLATES: Dict[str, str] = {
    "REG_MISMATCH": (
        "The most likely root cause is in the {module} module (confidence {confidence:.0%}). "
        "For MUL/DIV class mismatches this is typically a timing issue in the multi-cycle "
        "divider or a signed/unsigned operand handling error. "
        "Suspect lines: {suspect_lines}."
    ),
    "PC_MISMATCH": (
        "The root cause is most likely in {module} (confidence {confidence:.0%}). "
        "Branch mispredictions or jump-register calculation errors are common sources. "
        "Suspect lines: {suspect_lines}."
    ),
    "MEM_MISMATCH": (
        "The {module} module is the top root-cause candidate (confidence {confidence:.0%}). "
        "Store-to-load forwarding, write-buffer drain ordering, or byte-enable masking "
        "errors are typical causes. Suspect lines: {suspect_lines}."
    ),
    "CSR_MISMATCH": (
        "The {module} module is the most likely source (confidence {confidence:.0%}). "
        "CSR write-masking, privilege-level gating, or WARL field handling may be wrong. "
        "Suspect lines: {suspect_lines}."
    ),
    "TRAP_MISMATCH": (
        "The {module} module is the primary suspect (confidence {confidence:.0%}). "
        "Trap delegation registers (medeleg/mideleg), cause encoding, or mepc/mtval "
        "latching are common failure points. Suspect lines: {suspect_lines}."
    ),
    "ORDERMISMATCH": (
        "Ordering violations are typically in {module} (confidence {confidence:.0%}). "
        "Check the store buffer drain condition and fence instruction handling. "
        "Suspect lines: {suspect_lines}."
    ),
}

_ACTION_TEMPLATES: Dict[str, List[str]] = {
    "REG_MISMATCH": [
        "Review {module} for signed/unsigned multiplication or division corner cases.",
        "Run Agent G causal test generation targeting REG_MISMATCH to reproduce.",
        "Use Agent H minimizer to isolate the minimal failing instruction sequence.",
        "Add an intent spec for the specific register and operation that failed.",
    ],
    "PC_MISMATCH": [
        "Review branch target calculation in {module}, especially for JALR.",
        "Run additional branch-heavy directed tests from Agent G.",
        "Check return-address stack (RAS) depth and flush logic.",
        "Enable formal BMC via Agent L to bound the error depth.",
    ],
    "MEM_MISMATCH": [
        "Review store-to-load forwarding logic in {module}.",
        "Run Agent I litmus tests to check RVWMO compliance.",
        "Check byte-enable masking for SB/SH instructions.",
        "Add LR/SC AMO stress tests from Agent J reset-stress suite.",
    ],
    "CSR_MISMATCH": [
        "Audit {module} for WARL field masking on mstatus/mie/mip.",
        "Check privilege-mode transitions and CSR accessibility rules.",
        "Run Agent H intent checker with the CSR-mstatus-consistent spec.",
        "Add CSR read-write-verify sequences to the directed test suite.",
    ],
    "TRAP_MISMATCH": [
        "Audit trap delegation and cause encoding in {module}.",
        "Verify mepc points to the trapping instruction (not the next).",
        "Add nested-trap stress tests from Agent J.",
        "Run Agent H intent spec ecall-raises-trap on this test.",
    ],
    "ORDERMISMATCH": [
        "Run Agent I RVWMO litmus tests: store-load and fence patterns.",
        "Review store buffer drain condition in {module}.",
        "Add fence-rw-rw sequences around all AMO instructions in tests.",
        "Confirm LR/SC reservation granule is at least 64 bytes.",
    ],
    "EQUIV_FAIL": [
        "Review synthesis constraints for {module} — check for latch inference.",
        "Re-run Agent L equivalence check with increased BMC depth.",
        "Inspect the counterexample trace for the diverging signal.",
        "Add the counterexample as a formal seed via Agent H formal_fuzzer.",
    ],
    "CDC_VIOLATION": [
        "Add a two-flop synchroniser on signal {detail_signal}.",
        "Run sby CDC property check with --depth 20 for formal coverage.",
        "Review reset sequencing in Agent J cdc_report.json.",
    ],
    "INTENT_VIOLATION": [
        "Fix the RTL to conform to architectural invariant '{detail_spec}'.",
        "Add a directed test specifically for this invariant.",
        "Increase the intent checker's max_violations limit for full audit.",
    ],
}


# ─────────────────────────────────────────────────────────
# Explainer
# ─────────────────────────────────────────────────────────

class BugExplainer:
    """
    Generate structured explanations for AVA bug reports.

    Parameters
    ----------
    bug_report       : dict (bug_report.json)
    root_cause       : dict (root_cause.json from Agent H T8)
    intent_report    : dict (intent_report.json from T11), optional
    minimizer_stats  : dict with initial_length/final_length keys, optional
    """

    def __init__(
        self,
        bug_report:      Dict[str, Any],
        root_cause:      Optional[Dict[str, Any]] = None,
        intent_report:   Optional[Dict[str, Any]] = None,
        minimizer_stats: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.bug          = bug_report
        self.rc           = root_cause or {}
        self.intent       = intent_report or {}
        self.minimizer    = minimizer_stats or {}

    def _interpolate(self, template: str, **extra) -> str:
        """Fill template placeholders with bug report fields."""
        details = self.bug.get("details") or {}
        ctx = self.bug.get("rtl_context") or [{}]
        candidates = self.rc.get("candidates") or [{}]
        top_rc = candidates[0] if candidates else {}

        vals = {
            "seq":               self.bug.get("first_divergence_seq", "?"),
            "pc":                (ctx[0].get("pc") if ctx else None) or "?",
            "detail_register":   details.get("register") or details.get("signal") or "unknown",
            "detail_rtl_val":    details.get("rtl_value") or details.get("rtl_pc") or "?",
            "detail_iss_val":    details.get("iss_value") or details.get("iss_pc") or "?",
            "detail_address":    details.get("address") or "?",
            "detail_description": details.get("description") or "",
            "detail_spec":       "",
            "detail_depth":      details.get("depth") or "?",
            "detail_signal":     details.get("signal") or "?",
            "detail_from":       details.get("from_domain") or "?",
            "detail_to":         details.get("to_domain") or "?",
            "module":            top_rc.get("module") or "unknown module",
            "confidence":        top_rc.get("confidence") or 0.0,
            "suspect_lines":     ", ".join(str(l) for l in (top_rc.get("suspect_lines") or [])[:5]) or "unknown",
        }
        vals.update(extra)
        try:
            return template.format(**vals)
        except KeyError:
            return template

    def explain(self) -> Dict[str, Any]:
        mc = self.bug.get("mismatch_class", "REG_MISMATCH")
        run_id = self.bug.get("run_id", "unknown")

        symptom_tmpl = _SYMPTOM_TEMPLATES.get(mc, "A {mc} mismatch occurred at sequence {seq}.")
        symptom = self._interpolate(symptom_tmpl)

        rc_tmpl = _ROOT_CAUSE_TEMPLATES.get(mc, "")
        root_cause_text = self._interpolate(rc_tmpl) if rc_tmpl else "Root cause not determined."

        actions_raw = _ACTION_TEMPLATES.get(mc, ["Review the failing module."])
        actions = [self._interpolate(a) for a in actions_raw]

        # Minimizer summary
        min_summary = ""
        if self.minimizer:
            init = self.minimizer.get("initial_length", 0)
            final = self.minimizer.get("final_length", 0)
            pct  = self.minimizer.get("reduction_pct", 0)
            min_summary = (
                f"The minimal failing sequence was reduced from {init} to {final} "
                f"instructions ({pct:.1f}% reduction) by the delta-debugging minimizer."
            )

        summary = (
            f"[{mc}] Bug in run {run_id}: {symptom[:120]}..."
            if len(symptom) > 120 else f"[{mc}] Bug in run {run_id}: {symptom}"
        )

        technical = "\n\n".join(filter(None, [
            f"SYMPTOM\n{symptom}",
            f"ROOT CAUSE\n{root_cause_text}",
            f"MINIMIZATION\n{min_summary}" if min_summary else "",
        ]))

        return {
            "schema_version":    SCHEMA_VERSION,
            "agent":             "explainer",
            "run_id":            run_id,
            "mismatch_class":    mc,
            "summary":           summary,
            "symptom":           symptom,
            "root_cause":        root_cause_text,
            "minimizer_summary": min_summary,
            "recommended_actions": actions,
            "technical_detail":  technical,
            "generated_at":      datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }


def run_from_manifest(manifest_path: Path) -> int:
    with open(manifest_path) as f:
        manifest = json.load(f)

    run_dir = Path(manifest["run_dir"])
    outputs = manifest.get("outputs", {})

    def _load(key: str, default: str) -> Optional[Dict]:
        p = run_dir / (outputs.get(key) or default)
        if p.exists():
            with open(p) as f:
                return json.load(f)
        return None

    bug_report = _load("bug_report", "bug_report.json")
    if not bug_report:
        logger.warning("Explainer: no bug_report found, skipping")
        return 0

    root_cause    = _load("root_cause_report", "root_cause.json")
    intent_report = _load("intent_report",     "intent_report.json")

    explainer = BugExplainer(bug_report, root_cause, intent_report)
    report    = explainer.explain()
    report["run_id"] = manifest.get("run_id", "unknown")

    report_path = run_dir / "explanation.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
        f.write("\n")

    manifest.setdefault("outputs", {})["explanation"] = "explanation.json"
    tmp = manifest_path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    tmp.rename(manifest_path)

    logger.info("Explainer: generated explanation for %s", bug_report.get("mismatch_class"))
    return 0
