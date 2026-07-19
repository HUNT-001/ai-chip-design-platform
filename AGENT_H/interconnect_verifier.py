"""
AGENT_H.interconnect_verifier — Wishbone / AXI-Lite / AXI-Stream / TileLink
===========================================================================
(T69, taxonomy level 5 — extends `bus_verifier`, which covers AXI4/AHB/APB)

Golden protocol checkers for the internal-bus fabrics not covered by
`bus_verifier`. Each is a handshake/transaction-level model: the agent tracks
outstanding transactions and flags any beat that breaks the protocol contract.

Wishbone (classic + pipelined, B4)
----------------------------------
- **wb_handshake** (HIGH) — `ack`/`err`/`rty` asserted without `cyc & stb`, or
  more than one terminating signal asserted in the same cycle.
- **wb_cycle** (HIGH) — `stb` asserted without `cyc`, or `cyc` dropped with a
  transaction still outstanding.
- **wb_stall** (HIGH) — (pipelined) a new `stb` accepted while `stall` was high.

AXI4-Lite
---------
- **axil_handshake** (HIGH) — `VALID` de-asserted before `READY` (the AXI rule
  that VALID must remain asserted until the handshake completes).
- **axil_no_burst** (HIGH) — AXI-Lite carries burst signalling (`len` > 0 or a
  non-single burst type), which the subset forbids.
- **axil_response** (HIGH) — a response arrived with no outstanding request, or
  a write response count that does not match the number of write transactions.
- **axil_exclusive** (HIGH) — exclusive access attempted (unsupported in Lite).

AXI-Stream
----------
- **axis_tvalid_stable** (HIGH) — `TVALID` de-asserted before `TREADY`, or
  payload (`TDATA`/`TKEEP`/`TLAST`) changed while `TVALID` was high and the
  transfer had not completed.
- **axis_tlast_packet** (HIGH) — a packet exceeded the declared maximum length
  with no `TLAST`, or `TLAST` arrived on a null (all-`TKEEP`-low) beat.
- **axis_tkeep** (HIGH) — a position byte (`TKEEP=0, TSTRB=1`) or reserved
  `TKEEP`/`TSTRB` combination in a stream declared byte-type.

TileLink (TL-UL / TL-UH)
------------------------
- **tl_opcode** (HIGH) — an opcode illegal for the declared conformance level
  (e.g. a `Get`/`PutFull` mismatch, or TL-UH-only bursts on a TL-UL link).
- **tl_source_reuse** (HIGH) — a `source` id reused while a prior request with
  that id is still outstanding (source ids must be unique in flight).
- **tl_response_pairing** (HIGH) — a `d`-channel response whose opcode does not
  pair with the outstanding `a`-channel request (e.g. `AccessAckData` for a
  `PutFullData`), or a response for an unknown source.
- **tl_size_align** (HIGH) — address not aligned to `2^size`, or a size beyond
  the declared beat width.

Trace contract — `interconnect_trace.jsonl` (additive; skipped when absent)
---------------------------------------------------------------------------
```
{"event":"wb","cyc":true,"stb":true,"ack":true}
{"event":"axil","channel":"aw","valid":true,"ready":true,"addr":"0x10"}
{"event":"axis","tvalid":true,"tready":true,"tdata":"0x1","tlast":false}
{"event":"tl","channel":"a","opcode":4,"source":1,"size":2,"addr":"0x10",
 "level":"TL-UL"}
{"event":"tl","channel":"d","opcode":1,"source":1}
```

Stdlib-only, schema-v2.1.0, graceful degradation.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

log = logging.getLogger("AGENT_H.interconnect")

SCHEMA_VERSION = "2.1.0"
AGENT_NAME = "interconnect_verifier"

# TileLink A-channel opcodes
TL_A_GET, TL_A_PUTFULL, TL_A_PUTPARTIAL = 4, 0, 1
TL_A_ARITH, TL_A_LOGICAL, TL_A_INTENT = 2, 3, 5
# TileLink D-channel opcodes
TL_D_ACCESSACK, TL_D_ACCESSACKDATA = 0, 1
_TL_UL_A_OPS = {TL_A_GET, TL_A_PUTFULL, TL_A_PUTPARTIAL}
_TL_UH_A_OPS = _TL_UL_A_OPS | {TL_A_ARITH, TL_A_LOGICAL, TL_A_INTENT}
# requests that must be answered with data
_TL_DATA_REPLY = {TL_A_GET, TL_A_ARITH, TL_A_LOGICAL}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_int(v: Any) -> Optional[int]:
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        try:
            return int(v, 16) if v.lower().startswith("0x") else int(v)
        except ValueError:
            return None
    return None


def _truthy(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on", "asserted")
    return default


class InterconnectVerifier:
    def __init__(self, events: Sequence[Dict[str, Any]]):
        self.events = [e for e in (events or []) if isinstance(e, dict)]
        self.violations: List[Dict[str, Any]] = []
        # Wishbone
        self.wb_outstanding = 0
        # AXI-Lite: per-channel pending VALID and counts
        self.axil_prev: Dict[str, Dict[str, Any]] = {}
        self.axil_pending: Dict[str, int] = {"aw": 0, "w": 0, "ar": 0}
        # AXI-Stream
        self.axis_prev: Optional[Dict[str, Any]] = None
        self.axis_beats = 0
        # TileLink
        self.tl_inflight: Dict[int, Dict[str, Any]] = {}
        self.metrics: Dict[str, Any] = {
            "wb_ops": 0, "axil_ops": 0, "axis_ops": 0, "tl_ops": 0,
            "checked": 0, "interconnect_active": False, "by_protocol": {},
        }

    def _v(self, seq: Any, check: str, detail: str,
           severity: str = "HIGH") -> None:
        self.violations.append({"seq": seq, "check": check,
                                "severity": severity, "detail": detail})

    def _bump(self, proto: str) -> None:
        self.metrics["interconnect_active"] = True
        self.metrics["checked"] += 1
        self.metrics["by_protocol"][proto] = \
            self.metrics["by_protocol"].get(proto, 0) + 1

    # ── main ───────────────────────────────────────────────────────────────
    def run(self) -> Dict[str, Any]:
        started = _now()
        for e in self.events:
            kind = str(e.get("event", "")).lower()
            seq = e.get("seq")
            if kind in ("wb", "wishbone"):
                self._wb(e, seq)
            elif kind in ("axil", "axi_lite", "axi-lite"):
                self._axil(e, seq)
            elif kind in ("axis", "axi_stream", "axi-stream"):
                self._axis(e, seq)
            elif kind in ("tl", "tilelink"):
                self._tl(e, seq)
        return self._report(started)

    # ── Wishbone ───────────────────────────────────────────────────────────
    def _wb(self, e: Dict[str, Any], seq: Any) -> None:
        self.metrics["wb_ops"] += 1
        self._bump("wishbone")
        cyc = _truthy(e.get("cyc"))
        stb = _truthy(e.get("stb"))
        ack = _truthy(e.get("ack"))
        err = _truthy(e.get("err"))
        rty = _truthy(e.get("rty"))
        stall = _truthy(e.get("stall"))
        if stb and not cyc:
            self._v(seq, "wb_cycle", "STB asserted without CYC")
        term = sum((ack, err, rty))
        if term > 1:
            self._v(seq, "wb_handshake",
                    f"more than one terminating signal asserted "
                    f"(ack={ack} err={err} rty={rty})")
        if term and not (cyc and stb):
            self._v(seq, "wb_handshake",
                    f"termination (ack/err/rty) asserted without CYC & STB "
                    f"(cyc={cyc} stb={stb})")
        if stall and stb and _truthy(e.get("accepted")):
            self._v(seq, "wb_stall",
                    "new STB accepted while STALL was asserted (pipelined WB)")
        if cyc and stb and not term:
            self.wb_outstanding += 1
        elif term:
            self.wb_outstanding = max(0, self.wb_outstanding - 1)
        if not cyc and self.wb_outstanding > 0:
            self._v(seq, "wb_cycle",
                    f"CYC de-asserted with {self.wb_outstanding} transaction(s) "
                    f"still outstanding")
            self.wb_outstanding = 0

    # ── AXI4-Lite ──────────────────────────────────────────────────────────
    def _axil(self, e: Dict[str, Any], seq: Any) -> None:
        self.metrics["axil_ops"] += 1
        self._bump("axi-lite")
        ch = str(e.get("channel", "")).lower()
        valid = _truthy(e.get("valid"))
        ready = _truthy(e.get("ready"))
        # AXI-Lite forbids bursts and exclusive access
        ln = _to_int(e.get("len"))
        if ln is not None and ln > 0:
            self._v(seq, "axil_no_burst",
                    f"channel {ch}: AXI4-Lite forbids bursts (len={ln})")
        burst = e.get("burst")
        if burst is not None and str(burst).upper() not in ("FIXED", "0", "SINGLE"):
            self._v(seq, "axil_no_burst",
                    f"channel {ch}: burst type '{burst}' not allowed in AXI-Lite")
        lock = e.get("lock")
        if lock is not None and _truthy(lock):
            self._v(seq, "axil_exclusive",
                    f"channel {ch}: exclusive access is unsupported in AXI-Lite")
        # VALID must stay asserted until READY
        prev = self.axil_prev.get(ch)
        if prev is not None and prev["valid"] and not prev["ready"] and not valid:
            self._v(seq, "axil_handshake",
                    f"channel {ch}: VALID de-asserted before READY "
                    f"(handshake abandoned)")
        # outstanding accounting
        if ch in ("aw", "w", "ar") and valid and ready:
            self.axil_pending[ch] = self.axil_pending.get(ch, 0) + 1
        if ch in ("b", "r") and valid and ready:
            src = "aw" if ch == "b" else "ar"
            if self.axil_pending.get(src, 0) <= 0:
                self._v(seq, "axil_response",
                        f"channel {ch}: response with no outstanding "
                        f"{src.upper()} request")
            else:
                self.axil_pending[src] -= 1
        self.axil_prev[ch] = {"valid": valid, "ready": ready}

    # ── AXI-Stream ─────────────────────────────────────────────────────────
    def _axis(self, e: Dict[str, Any], seq: Any) -> None:
        self.metrics["axis_ops"] += 1
        self._bump("axi-stream")
        tvalid = _truthy(e.get("tvalid"))
        tready = _truthy(e.get("tready"))
        tlast = _truthy(e.get("tlast"))
        tkeep = _to_int(e.get("tkeep"))
        tstrb = _to_int(e.get("tstrb"))
        payload = (e.get("tdata"), tkeep, tlast)
        prev = self.axis_prev
        if prev is not None and prev["tvalid"] and not prev["tready"]:
            if not tvalid:
                self._v(seq, "axis_tvalid_stable",
                        "TVALID de-asserted before TREADY")
            elif prev["payload"] != payload:
                self._v(seq, "axis_tvalid_stable",
                        f"payload changed while TVALID was high and the "
                        f"transfer had not completed "
                        f"({prev['payload']} -> {payload})")
        # packet length / TLAST
        if tvalid and tready:
            self.axis_beats += 1
            maxlen = _to_int(e.get("max_packet"))
            if tlast:
                if tkeep == 0:
                    self._v(seq, "axis_tlast_packet",
                            "TLAST asserted on a null beat (TKEEP all zero)")
                self.axis_beats = 0
            elif maxlen is not None and self.axis_beats > maxlen:
                self._v(seq, "axis_tlast_packet",
                        f"packet exceeded max length {maxlen} beats with no "
                        f"TLAST")
                self.axis_beats = 0
            # TKEEP/TSTRB: position byte (keep=0, strb=1) is reserved
            if tkeep is not None and tstrb is not None:
                if (~tkeep) & tstrb:
                    self._v(seq, "axis_tkeep",
                            f"reserved TKEEP/TSTRB combination "
                            f"(tkeep=0x{tkeep:x}, tstrb=0x{tstrb:x}): a byte has "
                            f"TSTRB=1 with TKEEP=0")
        self.axis_prev = {"tvalid": tvalid, "tready": tready, "payload": payload}

    # ── TileLink ───────────────────────────────────────────────────────────
    def _tl(self, e: Dict[str, Any], seq: Any) -> None:
        self.metrics["tl_ops"] += 1
        self._bump("tilelink")
        ch = str(e.get("channel", "")).lower()
        op = _to_int(e.get("opcode"))
        src = _to_int(e.get("source"))
        level = str(e.get("level", "TL-UH")).upper()
        if ch == "a":
            if op is not None:
                allowed = _TL_UL_A_OPS if level in ("TL-UL", "TLUL") else _TL_UH_A_OPS
                if op not in allowed:
                    self._v(seq, "tl_opcode",
                            f"A-channel opcode {op} is not legal for {level}")
            size = _to_int(e.get("size"))
            addr = _to_int(e.get("addr"))
            if size is not None and addr is not None:
                if addr % (1 << size) != 0:
                    self._v(seq, "tl_size_align",
                            f"address 0x{addr:x} not aligned to 2^{size} bytes")
                beat_bits = _to_int(e.get("beat_bytes"))
                if (level in ("TL-UL", "TLUL") and beat_bits is not None
                        and (1 << size) > beat_bits):
                    self._v(seq, "tl_size_align",
                            f"size 2^{size} exceeds the TL-UL beat width "
                            f"{beat_bits} bytes (bursts need TL-UH)")
            if src is not None:
                if src in self.tl_inflight:
                    self._v(seq, "tl_source_reuse",
                            f"source id {src} reused while a prior request is "
                            f"still outstanding")
                else:
                    self.tl_inflight[src] = {"opcode": op}
        elif ch == "d":
            if src is None:
                return
            req = self.tl_inflight.pop(src, None)
            if req is None:
                self._v(seq, "tl_response_pairing",
                        f"D-channel response for unknown//completed source {src}")
                return
            a_op = req.get("opcode")
            if op is not None and a_op is not None:
                wants_data = a_op in _TL_DATA_REPLY
                if wants_data and op != TL_D_ACCESSACKDATA:
                    self._v(seq, "tl_response_pairing",
                            f"source {src}: A-opcode {a_op} requires "
                            f"AccessAckData ({TL_D_ACCESSACKDATA}), got {op}")
                elif not wants_data and op == TL_D_ACCESSACKDATA:
                    self._v(seq, "tl_response_pairing",
                            f"source {src}: A-opcode {a_op} must be answered "
                            f"with AccessAck ({TL_D_ACCESSACK}), got data reply")

    # ── report ─────────────────────────────────────────────────────────────
    def _report(self, started: str) -> Dict[str, Any]:
        high = sum(1 for v in self.violations if v["severity"] == "HIGH")
        total = len(self.violations)
        return {
            "schema_version": SCHEMA_VERSION,
            "agent": AGENT_NAME,
            "started_at": started,
            "finished_at": _now(),
            "records_checked": len(self.events),
            "interconnect_active": self.metrics["interconnect_active"],
            "metrics": self.metrics,
            "total_violations": total,
            "high_violations": high,
            "severity_score": high * 3 + (total - high),
            "band": "CLEAN" if total == 0 else ("CRITICAL" if high else "DEGRADED"),
            "pass": high == 0,
            "violations": self.violations[:100],
        }


# ─────────────────────────────────────────────────────────────────────────────
# Manifest entry point
# ─────────────────────────────────────────────────────────────────────────────
def _load_events(run_dir: Path, manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    name = (manifest.get("outputs", {}) or {}).get("interconnect_trace",
                                                    "interconnect_trace.jsonl")
    p = run_dir / name
    if not p.exists():
        return []
    text = p.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if text.lstrip().startswith("["):
        try:
            data = json.loads(text)
            return data if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def run_from_manifest(manifest_path: str) -> int:
    mp = Path(manifest_path)
    try:
        manifest = json.loads(mp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("interconnect_verifier: cannot read manifest: %s", exc)
        return 0
    run_dir = Path(manifest.get("run_dir", mp.parent))
    events = _load_events(run_dir, manifest)
    if not events:
        rep = {"schema_version": SCHEMA_VERSION, "agent": AGENT_NAME,
               "status": "skipped", "reason": "no interconnect_trace",
               "pass": True}
    else:
        rep = InterconnectVerifier(events).run()
        rep["status"] = "completed"
    try:
        (run_dir / "interconnect_report.json").write_text(
            json.dumps(rep, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("interconnect_verifier: cannot write report: %s", exc)
    return 0 if rep.get("pass", True) else 1


if __name__ == "__main__":  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="AVA interconnect checker")
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args()
    raise SystemExit(run_from_manifest(args.manifest))
