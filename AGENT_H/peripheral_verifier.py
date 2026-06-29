"""
AGENT_H/peripheral_verifier.py
==============================
T24 — SoC Peripheral Protocol Verification

Promotes the cross-domain *adapters* (``AGENT_H/cross_domain.py``) from simple
format shims into real **protocol checkers** with golden reference models and
scoreboards.  Where ``cross_domain`` only *translates* a DMA / UART / CRYPTO
DUT's output into the AVA commit-log schema, this module *verifies* that the
DUT obeyed its protocol contract.

Each checker consumes the DUT-specific raw record stream (the same format the
matching ``cross_domain`` adapter documents) and runs a stateful reference
model + scoreboard, emitting violations in the standard AVA report shape
(schema v2.1.0) so the existing report writers and confidence scorer need no
changes.

Domains
-------
  DMA    : channel FSM + byte-conservation scoreboard, null-pointer / zero-length
           / error-handling / dangling-channel checks.
  UART   : configure-before-use FSM, 8-bit data integrity, baud / parity
           sanity, parity-error-without-parity consistency.
  CRYPTO : key-before-op, status/output consistency (no result on ERROR),
           determinism scoreboard, AES encrypt/decrypt round-trip scoreboard,
           and a real SHA-256 known-answer test (golden via ``hashlib``).

Usage
-----
  from AGENT_H.peripheral_verifier import PeripheralVerifier, get_checker
  from AGENT_H.cross_domain import DUTClass

  report = PeripheralVerifier(raw_records, DUTClass.DMA).run()
  if not report["pass"]:
      ...

  # or from the pipeline:
  from AGENT_H.peripheral_verifier import run_from_manifest
  run_from_manifest(Path(run_dir) / "run_manifest.json")
"""

from __future__ import annotations

import hashlib
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "2.1.0"


# ─────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────

def _to_int(value: Any) -> Optional[int]:
    """Parse a hex string / int / None to an int."""
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


def _norm_hex(value: Any) -> Optional[str]:
    """Normalise a hex value to lower-case digits without the 0x prefix."""
    if value is None:
        return None
    if isinstance(value, str):
        v = value.strip().lower()
        if v.startswith("0x"):
            v = v[2:]
        return v or None
    if isinstance(value, int):
        return f"{value:x}"
    return None


@dataclass
class PeripheralViolation:
    check:       str
    severity:    str          # HIGH | MEDIUM | LOW
    seq:         int
    op:          Optional[str]
    description: str
    expected:    Optional[str] = None
    actual:      Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "check":       self.check,
            "severity":    self.severity,
            "seq":         self.seq,
            "op":          self.op,
            "description": self.description,
            "expected":    self.expected,
            "actual":      self.actual,
        }


# ─────────────────────────────────────────────────────────
# Base checker
# ─────────────────────────────────────────────────────────

class PeripheralChecker(ABC):
    """Base class for a stateful peripheral protocol checker."""

    domain: str = "base"

    def __init__(self) -> None:
        self.violations: List[PeripheralViolation] = []
        self.stats: Dict[str, int] = {}

    def _flag(self, v: PeripheralViolation) -> None:
        self.violations.append(v)

    def _bump(self, key: str, n: int = 1) -> None:
        self.stats[key] = self.stats.get(key, 0) + n

    @abstractmethod
    def check_record(self, raw: Dict[str, Any], seq: int) -> None:
        """Process one raw DUT record."""

    def finalize(self) -> None:
        """End-of-stream checks (default: none)."""
        return None


# ─────────────────────────────────────────────────────────
# DMA protocol checker
# ─────────────────────────────────────────────────────────

class DMAChecker(PeripheralChecker):
    """
    Reference model + scoreboard for DMA controllers.

    Raw record format (see ``cross_domain.DMAAdapter``)::

      {"channel": N, "op": "READ"|"WRITE"|"TRANSFER"|"DONE",
       "src_addr": "0x..", "dst_addr": "0x..", "length": N,
       "cycles": N, "error": false|true}

    Per-channel state machine + byte-conservation scoreboard:
      * READ  accumulates ``length`` bytes read   into the channel
      * WRITE  drains    ``length`` bytes written  from the channel
      * TRANSFER models a combined read→write of ``length`` bytes
      * DONE   requires read_bytes == write_bytes and clears the channel
    """
    domain = "dma"

    def __init__(self) -> None:
        super().__init__()
        # channel -> {"read": int, "write": int, "active": bool, "error": bool}
        self._ch: Dict[int, Dict[str, Any]] = {}

    def _chan(self, n: int) -> Dict[str, Any]:
        return self._ch.setdefault(n, {"read": 0, "write": 0, "active": False, "error": False})

    def check_record(self, raw: Dict[str, Any], seq: int) -> None:
        op  = str(raw.get("op", "")).upper()
        ch  = _to_int(raw.get("channel")) or 0
        st  = self._chan(ch)
        length = _to_int(raw.get("length"))
        src = _to_int(raw.get("src_addr"))
        dst = _to_int(raw.get("dst_addr"))
        err = bool(raw.get("error"))
        self._bump(op.lower() or "unknown")

        # an op on a channel already in the error state without a DONE/reset
        if st["error"] and op != "DONE":
            self._flag(PeripheralViolation(
                "dma_use_after_error", "HIGH", seq, op,
                f"channel {ch} issued {op} while in the error state (must reset/DONE first)"))

        if err:
            st["error"] = True

        if op in ("READ", "WRITE", "TRANSFER"):
            st["active"] = True
            if length is None or length <= 0:
                self._flag(PeripheralViolation(
                    "dma_bad_length", "HIGH", seq, op,
                    f"channel {ch} {op} with non-positive length",
                    expected=">0", actual=str(length)))
            if op in ("READ", "TRANSFER") and (src is None or src == 0):
                self._flag(PeripheralViolation(
                    "dma_null_src", "HIGH", seq, op,
                    f"channel {ch} {op} from null source address"))
            if op in ("WRITE", "TRANSFER") and (dst is None or dst == 0):
                self._flag(PeripheralViolation(
                    "dma_null_dst", "HIGH", seq, op,
                    f"channel {ch} {op} to null destination address"))
            if op == "TRANSFER" and src is not None and dst is not None and \
                    length and src != dst:
                # overlapping forward copy can corrupt data
                if abs(src - dst) < length:
                    self._flag(PeripheralViolation(
                        "dma_overlap", "MEDIUM", seq, op,
                        f"channel {ch} TRANSFER src/dst overlap "
                        f"(|{hex(src)}-{hex(dst)}| < {length})"))

            if length and length > 0:
                if op == "READ":
                    st["read"] += length
                elif op == "WRITE":
                    if st["write"] + length > st["read"] and st["read"] > 0:
                        self._flag(PeripheralViolation(
                            "dma_write_underflow", "HIGH", seq, op,
                            f"channel {ch} wrote more bytes than read "
                            f"({st['write'] + length} > {st['read']})"))
                    st["write"] += length
                else:  # TRANSFER counts both sides
                    st["read"]  += length
                    st["write"] += length

        elif op == "DONE":
            if not st["active"]:
                self._flag(PeripheralViolation(
                    "dma_spurious_done", "MEDIUM", seq, op,
                    f"channel {ch} DONE without any preceding transfer activity"))
            elif not st["error"] and st["read"] != st["write"]:
                self._flag(PeripheralViolation(
                    "dma_byte_mismatch", "HIGH", seq, op,
                    f"channel {ch} DONE with read/write byte mismatch",
                    expected=f"{st['read']} bytes", actual=f"{st['write']} bytes"))
            # clear channel
            self._ch[ch] = {"read": 0, "write": 0, "active": False, "error": False}

    def finalize(self) -> None:
        for ch, st in self._ch.items():
            if st["active"] and not st["error"]:
                self._flag(PeripheralViolation(
                    "dma_dangling_channel", "MEDIUM", -1, None,
                    f"channel {ch} left active without a DONE "
                    f"(read={st['read']}, write={st['write']})"))


# ─────────────────────────────────────────────────────────
# UART protocol checker
# ─────────────────────────────────────────────────────────

class UARTChecker(PeripheralChecker):
    """
    Reference model for UART controllers.

    Raw record format (see ``cross_domain.UARTAdapter``)::

      {"op": "TX"|"RX"|"CONFIG", "data": "0x..", "baud_rate": N,
       "parity": "NONE"|"EVEN"|"ODD",
       "framing_error": bool, "parity_error": bool, "cycles": N}
    """
    domain = "uart"

    _VALID_PARITY = {"NONE", "EVEN", "ODD"}

    def __init__(self) -> None:
        super().__init__()
        self._configured = False
        self._baud:   Optional[int] = None
        self._parity: str = "NONE"

    def check_record(self, raw: Dict[str, Any], seq: int) -> None:
        op = str(raw.get("op", "")).upper()
        self._bump(op.lower() or "unknown")

        if op == "CONFIG":
            baud   = _to_int(raw.get("baud_rate"))
            parity = str(raw.get("parity", "NONE")).upper()
            if baud is None or baud <= 0:
                self._flag(PeripheralViolation(
                    "uart_bad_baud", "HIGH", seq, op,
                    "CONFIG with non-positive baud rate",
                    expected=">0", actual=str(baud)))
            else:
                self._baud = baud
            if parity not in self._VALID_PARITY:
                self._flag(PeripheralViolation(
                    "uart_bad_parity", "MEDIUM", seq, op,
                    f"CONFIG with unknown parity mode {parity!r}",
                    expected="NONE|EVEN|ODD", actual=parity))
            else:
                self._parity = parity
            self._configured = True
            return

        if op in ("TX", "RX"):
            if not self._configured:
                self._flag(PeripheralViolation(
                    "uart_unconfigured_use", "MEDIUM", seq, op,
                    f"{op} issued before any CONFIG (baud/parity undefined)"))
            data = _to_int(raw.get("data"))
            if data is None:
                self._flag(PeripheralViolation(
                    "uart_no_data", "LOW", seq, op, f"{op} record carries no data byte"))
            elif data < 0 or data > 0xFF:
                self._flag(PeripheralViolation(
                    "uart_data_overflow", "HIGH", seq, op,
                    f"{op} data {hex(data)} exceeds an 8-bit frame",
                    expected="0x00..0xFF", actual=hex(data)))

            parity_err = bool(raw.get("parity_error"))
            if parity_err and self._parity == "NONE":
                self._flag(PeripheralViolation(
                    "uart_inconsistent_parity_error", "HIGH", seq, op,
                    "parity_error asserted while parity is disabled (NONE)"))
            if bool(raw.get("framing_error")):
                self._bump("framing_error")
            if parity_err:
                self._bump("parity_error")


# ─────────────────────────────────────────────────────────
# CRYPTO protocol checker
# ─────────────────────────────────────────────────────────

class CryptoChecker(PeripheralChecker):
    """
    Reference model + scoreboard for crypto accelerators.

    Raw record format (see ``cross_domain.CryptoAdapter``)::

      {"op": "AES_ENC"|"AES_DEC"|"SHA256"|"RSA_MOD",
       "key_addr": "0x..", "data_in": "0x..", "data_out": "0x..",
       "cycles": N, "status": "DONE"|"ERROR", "error_code": N}

    Checks:
      * key-before-op for keyed primitives (AES / RSA)
      * status/output consistency: ERROR must not expose a result; DONE must
        produce one (no-result / leak detection)
      * determinism: identical (op, key, input) must yield identical output
      * AES encrypt→decrypt round-trip scoreboard
      * SHA-256 known-answer test (real golden digest via hashlib)
    """
    domain = "crypto"

    _KEYED = {"AES_ENC", "AES_DEC", "RSA_MOD"}

    def __init__(self) -> None:
        super().__init__()
        # (op, key, data_in) -> data_out  for determinism
        self._seen: Dict[Tuple[str, str, str], str] = {}
        # (key, plaintext) -> ciphertext  for AES round-trip
        self._aes_enc: Dict[Tuple[str, str], str] = {}

    def check_record(self, raw: Dict[str, Any], seq: int) -> None:
        op     = str(raw.get("op", "")).upper()
        status = str(raw.get("status", "DONE")).upper()
        key    = _norm_hex(raw.get("key_addr"))
        din    = _norm_hex(raw.get("data_in"))
        dout   = _norm_hex(raw.get("data_out"))
        self._bump(op.lower() or "unknown")

        # key-before-op
        if op in self._KEYED and (key is None or key == "0" or key == ""):
            self._flag(PeripheralViolation(
                "crypto_no_key", "HIGH", seq, op,
                f"{op} issued without a key (key_addr null)"))

        # status / output consistency
        if status == "ERROR":
            if dout and dout not in ("0", "00"):
                self._flag(PeripheralViolation(
                    "crypto_error_with_output", "HIGH", seq, op,
                    f"{op} reported ERROR but still exposed a result "
                    f"(potential information leak)",
                    expected="no output", actual="0x" + dout))
            return  # do not score an errored op further
        else:
            if op != "RSA_MOD" and (dout is None or dout in ("", "0")):
                self._flag(PeripheralViolation(
                    "crypto_no_output", "MEDIUM", seq, op,
                    f"{op} reported DONE but produced no output"))

        # determinism
        if din is not None and dout is not None:
            dk = (op, key or "", din)
            prev = self._seen.get(dk)
            if prev is not None and prev != dout:
                self._flag(PeripheralViolation(
                    "crypto_nondeterministic", "HIGH", seq, op,
                    f"{op} produced different output for identical key+input",
                    expected="0x" + prev, actual="0x" + dout))
            else:
                self._seen[dk] = dout

        # AES round-trip scoreboard
        if op == "AES_ENC" and din and dout and key:
            self._aes_enc[(key, din)] = dout
        elif op == "AES_DEC" and din and dout and key:
            # din is ciphertext; look for a matching prior encryption
            for (k, pt), ct in self._aes_enc.items():
                if k == key and ct == din:
                    if dout != pt:
                        self._flag(PeripheralViolation(
                            "aes_roundtrip", "HIGH", seq, op,
                            "AES decrypt did not recover the original plaintext",
                            expected="0x" + pt, actual="0x" + dout))
                    break

        # SHA-256 known-answer test (golden reference)
        if op == "SHA256" and din is not None and dout is not None:
            self._check_sha256(din, dout, seq)

    def _check_sha256(self, din: str, dout: str, seq: int) -> None:
        # only attempt when the input is byte-aligned hex and the output looks
        # like a 256-bit digest (64 hex chars)
        msg = din[1:] if len(din) % 2 else din  # drop a stray nibble if present
        if len(msg) % 2 != 0:
            return
        try:
            golden = hashlib.sha256(bytes.fromhex(msg)).hexdigest()
        except ValueError:
            return
        if len(dout) == 64 and dout != golden:
            self._flag(PeripheralViolation(
                "sha256_kat", "HIGH", seq, "SHA256",
                "SHA-256 digest does not match the golden reference",
                expected="0x" + golden, actual="0x" + dout))


# ─────────────────────────────────────────────────────────
# Registry / factory
# ─────────────────────────────────────────────────────────

_CHECKER_REGISTRY: Dict[str, type] = {
    "dma":    DMAChecker,
    "uart":   UARTChecker,
    "crypto": CryptoChecker,
}


def register_checker(dut_class: str, checker_cls: type) -> None:
    """Register a custom peripheral checker for a DUT class name."""
    _CHECKER_REGISTRY[str(dut_class).lower()] = checker_cls
    logger.info("Registered peripheral checker %s for %s", checker_cls.__name__, dut_class)


def get_checker(dut_class: Any) -> Optional[PeripheralChecker]:
    """
    Return a fresh checker instance for the given DUT class, or ``None`` if the
    class has no protocol checker (e.g. ``cpu`` / ``custom``).
    """
    name = getattr(dut_class, "value", dut_class)
    cls = _CHECKER_REGISTRY.get(str(name).lower())
    return cls() if cls else None


# ─────────────────────────────────────────────────────────
# Verifier
# ─────────────────────────────────────────────────────────

class PeripheralVerifier:
    """
    Run the appropriate protocol checker over a raw peripheral record stream.

    Parameters
    ----------
    records   : list of raw DUT records (DUT-specific format)
    dut_class : DUTClass enum value or string ("dma" | "uart" | "crypto")
    """

    _SEV_WEIGHT = {"HIGH": 1.0, "MEDIUM": 0.4, "LOW": 0.15}

    def __init__(self, records: List[Dict[str, Any]], dut_class: Any) -> None:
        self.records   = records or []
        self.dut_class = str(getattr(dut_class, "value", dut_class)).lower()
        self.checker   = get_checker(self.dut_class)

    def run(self) -> Dict[str, Any]:
        started = datetime.now(timezone.utc)

        if self.checker is None:
            finished = datetime.now(timezone.utc)
            return {
                "schema_version": SCHEMA_VERSION,
                "agent":          "peripheral_verifier",
                "dut_class":      self.dut_class,
                "status":         "skipped",
                "reason":         f"no protocol checker for DUT class {self.dut_class!r}",
                "records_checked": 0,
                "total_violations": 0,
                "pass":           True,
                "violations":     [],
                "band":           "CLEAN",
                "started_at":     started.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "finished_at":    finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "duration_s":     0.0,
            }

        for i, rec in enumerate(self.records):
            seq = rec.get("seq", i) if isinstance(rec, dict) else i
            try:
                self.checker.check_record(rec, seq)
            except Exception as exc:          # never crash the pipeline
                logger.warning("peripheral_verifier: record %d raised: %s", seq, exc)
        try:
            self.checker.finalize()
        except Exception as exc:
            logger.warning("peripheral_verifier: finalize raised: %s", exc)

        finished = datetime.now(timezone.utc)
        return self._report(started, finished)

    def _band(self) -> Tuple[float, str]:
        viols = self.checker.violations
        if not viols:
            return 0.0, "CLEAN"
        score   = sum(self._SEV_WEIGHT.get(v.severity, 0.2) for v in viols)
        norm    = min(1.0, score / max(1, len(self.records)))
        if any(v.severity == "HIGH" for v in viols):
            band = "CRITICAL"
        elif norm >= 0.3:
            band = "DEGRADED"
        else:
            band = "MINOR"
        return round(norm, 4), band

    def _report(self, started, finished) -> Dict[str, Any]:
        viols = self.checker.violations
        score, band = self._band()
        high = [v for v in viols if v.severity == "HIGH"]
        return {
            "schema_version":   SCHEMA_VERSION,
            "agent":            "peripheral_verifier",
            "dut_class":        self.dut_class,
            "status":           "completed",
            "records_checked":  len(self.records),
            "stats":            dict(self.checker.stats),
            "total_violations": len(viols),
            "high_violations":  len(high),
            "severity_score":   score,
            "band":             band,
            "pass":             len(viols) == 0,
            "violations":       [v.to_dict() for v in viols[:50]],
            "started_at":       started.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "finished_at":      finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "duration_s":       round((finished - started).total_seconds(), 3),
        }


# ─────────────────────────────────────────────────────────
# Manifest integration
# ─────────────────────────────────────────────────────────

def _load_raw_log(run_dir: Path, outputs: Dict, key: str, default: str) -> List[Dict]:
    p = run_dir / (outputs.get(key) or default)
    if not p.exists():
        return []
    recs: List[Dict] = []
    with open(p) as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    recs.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return recs


def run_from_manifest(manifest_path: Path) -> int:
    """
    Pipeline entry point.  Reads the DUT class from ``agent_config.dut_class``;
    for non-CPU peripherals it loads the raw DUT log and runs the protocol
    checker, writing ``peripheral_report.json`` and updating the manifest.
    Returns 0 on pass / skip, 1 on any violation.
    """
    manifest_path = Path(manifest_path)
    try:
        with open(manifest_path) as f:
            manifest = json.load(f)
    except Exception as exc:
        logger.warning("peripheral_verifier: cannot read manifest: %s", exc)
        return 0

    dut_class = str(manifest.get("agent_config", {}).get("dut_class", "cpu")).lower()
    if dut_class in ("cpu", "custom"):
        logger.info("peripheral_verifier: DUT class %s has no protocol checker, skipping", dut_class)
        return 0

    run_dir = Path(manifest.get("run_dir", manifest_path.parent))
    outputs = manifest.get("outputs", {})
    records = _load_raw_log(run_dir, outputs, "raw_rtl_log", "raw_rtl.jsonl")
    if not records:
        logger.info("peripheral_verifier: no raw DUT log found, skipping")
        return 0

    report = PeripheralVerifier(records, dut_class).run()

    report_path = run_dir / "peripheral_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)
        f.write("\n")

    manifest.setdefault("outputs", {})["peripheral_report"] = "peripheral_report.json"
    manifest.setdefault("phases", {})["peripheral_check"] = {
        "status": "pass" if report["pass"] else "fail",
        "violations": report["total_violations"],
    }
    tmp = manifest_path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    tmp.rename(manifest_path)

    logger.info("peripheral_verifier: %s %d records, %d violations, band=%s",
                dut_class, report["records_checked"],
                report["total_violations"], report["band"])
    return 0 if report["pass"] else 1


# ─────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────

if __name__ == "__main__":          # pragma: no cover
    import argparse

    ap = argparse.ArgumentParser(description="SoC peripheral protocol verifier")
    ap.add_argument("--manifest", type=Path, help="run_manifest.json path")
    ap.add_argument("--raw", type=Path, help="raw DUT log (JSONL)")
    ap.add_argument("--dut-class", choices=("dma", "uart", "crypto"),
                    help="DUT class for --raw mode")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.manifest:
        raise SystemExit(run_from_manifest(args.manifest))
    if args.raw and args.dut_class:
        log = []
        with open(args.raw) as fh:
            for ln in fh:
                ln = ln.strip()
                if ln:
                    log.append(json.loads(ln))
        rep = PeripheralVerifier(log, args.dut_class).run()
        print(json.dumps(rep, indent=2))
        raise SystemExit(0 if rep["pass"] else 1)
    ap.print_help()
