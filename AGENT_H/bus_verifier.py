"""
AGENT_H/bus_verifier.py
=======================
T34 — Bus Protocol Verification (AXI4 / AHB / APB)

Verifies on-chip bus transactions against a golden transaction-level protocol
model.  The novel core is a precise AXI4 burst generator (`axi_expected_beats`)
that, from a transaction descriptor (address, length, size, burst type),
produces the exact beat-address sequence the protocol mandates — which the
checker compares against the beats the DUT actually drove.  It also enforces the
purely-arithmetic AXI rules that are notorious bug sources: the 4 KB-boundary
prohibition, WRAP power-of-two / alignment constraints, ``WLAST`` placement and
response-code validity.

This pairs naturally with the cache verifier: a write-back cache's eviction
traffic *is* a sequence of bus bursts.

Checks
------
  bus_burst_length   number of beats != AxLEN + 1
  bus_wlast          the "last" flag is not on (exactly) the final beat
  bus_beat_addr      a beat address != the protocol-mandated address
  bus_4kb_boundary   an INCR/FIXED burst crosses a 4 KB boundary
  bus_wrap_invalid   WRAP length not in {2,4,8,16} or start not size-aligned
  bus_resp           response code not valid for the protocol

Metrics (analytics — never fail the run)
----------------------------------------
  transactions, reads, writes, beats, error_responses

Conservative gating (no false positives)
----------------------------------------
  Each check fires only for the descriptor fields a transaction actually
  provides (beat-level checks need a ``beats`` list; burst checks need
  ``len``/``size``/``burst``).  A transaction with an unknown protocol or
  missing fields is counted in the metrics but not failed.

Optional trace contract (additive only)
---------------------------------------
  A bus transaction (in a ``bus_trace`` file, or a record's ``bus`` field —
  a dict or a list of dicts)::

    {
      "protocol": "axi4"|"axi4lite"|"ahb"|"apb",
      "id": 0, "txn": "write"|"read",
      "addr": "0x..",
      "len":  3,            # AxLEN  (beats - 1)
      "size": 2,            # AxSIZE (bytes per beat = 2^size)
      "burst": "fixed"|"incr"|"wrap",
      "beats": [ {"addr":"0x..","data":"0x..","last":true}, ... ],
      "resp": "okay"|"exokay"|"slverr"|"decerr"
    }

Usage
-----
  from AGENT_H.bus_verifier import BusVerifier, axi_expected_beats
  report = BusVerifier(transactions).run()

  from AGENT_H.bus_verifier import run_from_manifest
  run_from_manifest(Path(run_dir) / "run_manifest.json")
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "2.1.0"
_M32 = 0xFFFFFFFF

_VALID_RESP = {
    "axi4":     {"okay", "exokay", "slverr", "decerr"},
    "axi4lite": {"okay", "slverr", "decerr"},
    "ahb":      {"okay", "error"},
    "apb":      {"okay", "error"},
}
_WRAP_LENGTHS = {2, 4, 8, 16}


def _to_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        v = value.strip()
        if not v:
            return None
        try:
            return int(v, 16) if v.lower().startswith("0x") else int(v, 0)
        except ValueError:
            try:
                return int(v)
            except ValueError:
                return None
    return None


# ─────────────────────────────────────────────────────────
# Golden AXI burst model
# ─────────────────────────────────────────────────────────

def axi_expected_beats(addr: int, length: int, size: int,
                       burst: str) -> List[Tuple[int, bool]]:
    """
    Return the mandated [(beat_addr, is_last)] sequence for an AXI burst.

    addr   : start address
    length : AxLEN  (number of beats - 1)
    size   : AxSIZE (bytes/beat = 2**size)
    burst  : "fixed" | "incr" | "wrap"
    """
    n = length + 1
    nbytes = 1 << size
    b = (burst or "incr").lower()
    addr &= _M32
    out: List[Tuple[int, bool]] = []
    if b == "fixed":
        for i in range(n):
            out.append((addr, i == n - 1))
    elif b == "wrap":
        total = n * nbytes
        boundary = addr - (addr % total) if total else addr
        for i in range(n):
            a = (boundary + ((addr - boundary + i * nbytes) % total)) & _M32 if total else addr
            out.append((a, i == n - 1))
    else:  # incr (default)
        for i in range(n):
            out.append(((addr + i * nbytes) & _M32, i == n - 1))
    return out


def crosses_4kb(addr: int, length: int, size: int, burst: str) -> bool:
    """True if an INCR/FIXED burst crosses a 4 KB boundary (WRAP never does)."""
    if (burst or "incr").lower() == "wrap":
        return False
    n = length + 1
    nbytes = 1 << size
    if (burst or "incr").lower() == "fixed":
        last_byte = addr + nbytes - 1
    else:
        last_byte = addr + n * nbytes - 1
    return (addr >> 12) != (last_byte >> 12)


# ─────────────────────────────────────────────────────────
# Violation
# ─────────────────────────────────────────────────────────

@dataclass
class BusViolation:
    check:       str
    severity:    str
    seq:         int
    protocol:    Optional[str]
    description: str
    expected:    Optional[str] = None
    actual:      Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "check": self.check, "severity": self.severity, "seq": self.seq,
            "protocol": self.protocol, "description": self.description,
            "expected": self.expected, "actual": self.actual,
        }


# ─────────────────────────────────────────────────────────
# Verifier
# ─────────────────────────────────────────────────────────

class BusVerifier:
    """
    Verify a list of bus transactions against the golden protocol model.

    Parameters
    ----------
    transactions   : list of transaction descriptor dicts (see module contract)
    max_violations : stop collecting after this many violations
    """

    _SEV_WEIGHT = {"HIGH": 1.0, "MEDIUM": 0.4, "LOW": 0.15}

    def __init__(
        self,
        transactions:   List[Dict[str, Any]],
        max_violations: int = 200,
    ) -> None:
        self.transactions   = transactions or []
        self.max_violations = max_violations
        self._violations: List[BusViolation] = []
        self._stats = {"transactions": 0, "reads": 0, "writes": 0,
                       "beats": 0, "error_responses": 0, "checked": 0}

    def _flag(self, v: BusViolation) -> None:
        if len(self._violations) < self.max_violations:
            self._violations.append(v)

    def _check_txn(self, txn: Dict, seq: int) -> None:
        if not isinstance(txn, dict):
            return
        proto = str(txn.get("protocol", "axi4")).lower()
        self._stats["transactions"] += 1
        if str(txn.get("txn", "")).lower() == "read":
            self._stats["reads"] += 1
        elif str(txn.get("txn", "")).lower() == "write":
            self._stats["writes"] += 1

        # response-code validity
        resp = txn.get("resp")
        if resp is not None:
            r = str(resp).lower()
            valid = _VALID_RESP.get(proto, _VALID_RESP["axi4"])
            if r not in valid:
                self._flag(BusViolation(
                    "bus_resp", "HIGH", seq, proto,
                    f"invalid {proto} response code {resp!r}",
                    expected="|".join(sorted(valid)), actual=str(resp)))
            elif r in ("slverr", "decerr", "error"):
                self._stats["error_responses"] += 1

        addr = _to_int(txn.get("addr"))
        length = _to_int(txn.get("len"))
        size = _to_int(txn.get("size"))
        burst = str(txn.get("burst", "incr")).lower()
        beats = txn.get("beats")

        # burst-parameter checks (need addr/len/size)
        if addr is not None and length is not None and size is not None:
            self._stats["checked"] += 1
            if burst == "wrap":
                n = length + 1
                if n not in _WRAP_LENGTHS:
                    self._flag(BusViolation(
                        "bus_wrap_invalid", "HIGH", seq, proto,
                        f"WRAP burst length {n} not in {{2,4,8,16}}",
                        expected="2|4|8|16", actual=str(n)))
                elif addr % (1 << size) != 0:
                    self._flag(BusViolation(
                        "bus_wrap_invalid", "HIGH", seq, proto,
                        f"WRAP start 0x{addr:08x} not aligned to {1 << size} bytes",
                        expected=f"align {1 << size}", actual=f"0x{addr:08x}"))
            if crosses_4kb(addr, length, size, burst):
                last = addr + (length + 1) * (1 << size) - 1
                self._flag(BusViolation(
                    "bus_4kb_boundary", "HIGH", seq, proto,
                    f"{burst.upper()} burst 0x{addr:08x}..0x{last:08x} crosses a 4 KB boundary",
                    expected="single 4KB page", actual=f"0x{addr:08x}..0x{last:08x}"))

        # beat-level checks (need the observed beats + descriptor)
        if isinstance(beats, list) and addr is not None and length is not None \
                and size is not None:
            self._stats["beats"] += len(beats)
            expected = axi_expected_beats(addr, length, size, burst)
            if len(beats) != len(expected):
                self._flag(BusViolation(
                    "bus_burst_length", "HIGH", seq, proto,
                    f"{len(beats)} beats observed but AxLEN={length} implies {len(expected)}",
                    expected=str(len(expected)), actual=str(len(beats))))
            for i, beat in enumerate(beats):
                if i >= len(expected) or not isinstance(beat, dict):
                    break
                exp_addr, exp_last = expected[i]
                ba = _to_int(beat.get("addr"))
                if ba is not None and ba != exp_addr:
                    self._flag(BusViolation(
                        "bus_beat_addr", "HIGH", seq, proto,
                        f"beat {i} address 0x{ba:08x} != mandated 0x{exp_addr:08x} "
                        f"({burst.upper()})",
                        expected=f"0x{exp_addr:08x}", actual=f"0x{ba:08x}"))
                bl = beat.get("last")
                if isinstance(bl, bool) and bl != exp_last:
                    self._flag(BusViolation(
                        "bus_wlast", "HIGH", seq, proto,
                        f"beat {i} LAST={bl} but the final beat is index {len(expected) - 1}",
                        expected=str(exp_last), actual=str(bl)))

    # -- main loop ------------------------------------------------------------

    def run(self) -> Dict[str, Any]:
        started = datetime.now(timezone.utc)
        n = len(self.transactions)
        for i, txn in enumerate(self.transactions):
            if len(self._violations) >= self.max_violations:
                break
            try:
                self._check_txn(txn, txn.get("seq", i) if isinstance(txn, dict) else i)
            except Exception as exc:               # never crash the pipeline
                logger.warning("bus_verifier: txn %d raised: %s", i, exc)
        finished = datetime.now(timezone.utc)
        return self._report(n, started, finished)

    # -- reporting ------------------------------------------------------------

    def _band(self) -> Tuple[float, str]:
        if not self._violations:
            return 0.0, "CLEAN"
        score = sum(self._SEV_WEIGHT.get(v.severity, 0.2) for v in self._violations)
        norm  = min(1.0, score / max(1, self._stats["checked"] + 1))
        if any(v.severity == "HIGH" for v in self._violations):
            band = "CRITICAL"
        elif norm >= 0.3:
            band = "DEGRADED"
        else:
            band = "MINOR"
        return round(norm, 4), band

    def _report(self, n: int, started, finished) -> Dict[str, Any]:
        score, band = self._band()
        high = [v for v in self._violations if v.severity == "HIGH"]
        return {
            "schema_version":   SCHEMA_VERSION,
            "agent":            "bus_verifier",
            "transactions":     n,
            "metrics":          dict(self._stats),
            "total_violations": len(self._violations),
            "high_violations":  len(high),
            "severity_score":   score,
            "band":             band,
            "pass":             len(self._violations) == 0,
            "violations":       [v.to_dict() for v in self._violations[:50]],
            "started_at":       started.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "finished_at":      finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration_s":       round((finished - started).total_seconds(), 3),
        }


# ─────────────────────────────────────────────────────────
# Manifest integration
# ─────────────────────────────────────────────────────────

def _load_jsonl(path: Path) -> List[Dict]:
    if not path.exists():
        return []
    out: List[Dict] = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


def _extract_bus(rtl_log: List[Dict]) -> List[Dict]:
    """Collect transactions from a commit log's per-record ``bus`` fields."""
    txns: List[Dict] = []
    for rec in rtl_log:
        if not isinstance(rec, dict):
            continue
        b = rec.get("bus")
        if isinstance(b, dict):
            txns.append(b)
        elif isinstance(b, list):
            txns.extend(x for x in b if isinstance(x, dict))
    return txns


def run_from_manifest(manifest_path: Path) -> int:
    """Pipeline entry point. Returns 0 on pass, 1 on any violation."""
    manifest_path = Path(manifest_path)
    try:
        with open(manifest_path) as f:
            manifest = json.load(f)
    except Exception as exc:
        logger.warning("bus_verifier: cannot read manifest: %s", exc)
        return 0

    run_dir = Path(manifest.get("run_dir", manifest_path.parent))
    outputs = manifest.get("outputs", {})

    txns = _load_jsonl(run_dir / (outputs.get("bus_trace") or "bus_trace.jsonl"))
    if not txns:
        rtl_log = _load_jsonl(run_dir / (outputs.get("rtl_commit_log") or "rtl_commit.jsonl"))
        txns = _extract_bus(rtl_log)
    if not txns:
        logger.info("bus_verifier: no bus transactions found, skipping")
        return 0

    report = BusVerifier(txns).run()

    report_path = run_dir / "bus_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
        f.write("\n")

    manifest.setdefault("outputs", {})["bus_report"] = "bus_report.json"
    manifest.setdefault("phases", {})["bus_check"] = {
        "status": "pass" if report["pass"] else "fail",
        "violations": report["total_violations"],
    }
    tmp = manifest_path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    tmp.rename(manifest_path)

    logger.info("bus_verifier: %d transactions, %d violations, band=%s",
                report["transactions"], report["total_violations"], report["band"])
    return 0 if report["pass"] else 1


# ─────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":          # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description="Bus protocol verifier (AXI/AHB/APB)")
    ap.add_argument("--manifest", type=Path)
    ap.add_argument("--bus", type=Path, help="bus_trace.jsonl")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.manifest:
        raise SystemExit(run_from_manifest(args.manifest))
    if args.bus:
        rep = BusVerifier(_load_jsonl(args.bus)).run()
        print(json.dumps(rep, indent=2))
        raise SystemExit(0 if rep["pass"] else 1)
    ap.print_help()
