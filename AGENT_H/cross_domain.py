"""
AGENT_H/cross_domain.py
========================
T22 — Cross-Domain Adapter Interfaces

Provides adapter interfaces that allow AVA's verification pipeline to work
with non-CPU DUTs (Design Under Test) beyond the primary RISC-V CPU target.

Supported DUT classes
---------------------
  CPU       : default (RISC-V RV32IM) — no adapter needed
  CRYPTO    : AES/SHA/RSA crypto accelerators
  DMA       : Direct Memory Access controllers
  UART      : Serial interface controllers
  CUSTOM    : User-defined DUT with custom commit-log format

Each adapter translates the DUT-specific output format into the AVA canonical
commit-log schema (commitlog.schema.json v2.1.0) so that Agents D, F, H etc.
can analyse the output without modification.

Usage
-----
  from AGENT_H.cross_domain import get_adapter, DUTClass

  adapter = get_adapter(DUTClass.CRYPTO)
  canonical_log = adapter.translate(raw_dut_output)
  # canonical_log is a list of commit-log dicts in AVA schema format
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Type

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "2.1.0"


# ─────────────────────────────────────────────────────────
# DUT class registry
# ─────────────────────────────────────────────────────────

class DUTClass(str, Enum):
    CPU    = "cpu"
    CRYPTO = "crypto"
    DMA    = "dma"
    UART   = "uart"
    CUSTOM = "custom"


# ─────────────────────────────────────────────────────────
# Base adapter
# ─────────────────────────────────────────────────────────

class DUTAdapter(ABC):
    """
    Abstract base class for cross-domain DUT adapters.

    Subclasses implement ``translate_record`` to convert one DUT-specific
    output record into an AVA commit-log record.
    """

    dut_class: DUTClass = DUTClass.CUSTOM
    name:      str      = "base"

    @abstractmethod
    def translate_record(self, raw: Dict[str, Any], seq: int) -> Dict[str, Any]:
        """
        Convert a raw DUT output record to AVA commit-log format.

        Parameters
        ----------
        raw : dict — raw DUT output (format is DUT-specific)
        seq : int  — sequence number to assign

        Returns
        -------
        dict conforming to commitlog.schema.json v2.1.0
        """

    def translate(self, raw_records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Translate a list of raw DUT records to AVA commit-log format."""
        result = []
        for i, rec in enumerate(raw_records):
            try:
                translated = self.translate_record(rec, i)
                result.append(translated)
            except Exception as exc:
                logger.warning("Adapter %s: failed to translate record %d: %s",
                               self.name, i, exc)
        return result

    def translate_file(
        self,
        input_path:  Path,
        output_path: Path,
        fmt:         str = "jsonl",
    ) -> int:
        """
        Translate a raw DUT output file to AVA commit-log JSONL.

        Parameters
        ----------
        input_path  : path to raw DUT output file (JSONL or JSON array)
        output_path : path to write translated AVA commit-log JSONL
        fmt         : "jsonl" (one JSON per line) or "json" (JSON array)

        Returns number of records translated.
        """
        raw_records: List[Dict] = []
        with open(input_path) as f:
            if fmt == "json":
                raw_records = json.load(f)
            else:
                for line in f:
                    line = line.strip()
                    if line:
                        raw_records.append(json.loads(line))

        translated = self.translate(raw_records)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "w") as f:
            for rec in translated:
                f.write(json.dumps(rec) + "\n")
        logger.info("Adapter %s: translated %d records → %s", self.name, len(translated), output_path)
        return len(translated)

    @staticmethod
    def _base_record(seq: int, pc: int = 0, disasm: str = "nop") -> Dict[str, Any]:
        """Return a minimal valid AVA commit-log record."""
        return {
            "schema_version": SCHEMA_VERSION,
            "seq":            seq,
            "pc":             f"0x{pc:08x}",
            "disasm":         disasm,
            "regs":           {},
            "csrs":           {},
        }


# ─────────────────────────────────────────────────────────
# Crypto accelerator adapter
# ─────────────────────────────────────────────────────────

class CryptoAdapter(DUTAdapter):
    """
    Adapter for AES/SHA/RSA crypto accelerators.

    Expected raw record format:
      {
        "op":        "AES_ENC" | "AES_DEC" | "SHA256" | "RSA_MOD",
        "key_addr":  "0x...",
        "data_in":   "0x...",
        "data_out":  "0x...",
        "cycles":    N,
        "status":    "DONE" | "ERROR",
        "error_code": N  (optional)
      }
    """
    dut_class = DUTClass.CRYPTO
    name      = "crypto_adapter"

    def translate_record(self, raw: Dict, seq: int) -> Dict:
        rec = self._base_record(seq, pc=seq * 4, disasm=raw.get("op", "crypto_op"))
        rec["regs"] = {
            "a0": raw.get("data_out", "0x0"),   # result register
            "a1": raw.get("data_in",  "0x0"),   # input
        }
        rec["csrs"] = {
            "mstatus": "0x1800",  # M-mode
        }
        # Encode status as a trap-like record
        if raw.get("status") == "ERROR":
            rec["trap"] = {
                "cause":   raw.get("error_code", 0),
                "tval":    raw.get("key_addr", "0x0"),
            }
        # Perf: cycles per operation
        cycles = raw.get("cycles")
        if cycles is not None:
            rec["perf_counters"] = {
                "cycles":  cycles,
                "instret": 1,
            }
        return rec


# ─────────────────────────────────────────────────────────
# DMA controller adapter
# ─────────────────────────────────────────────────────────

class DMAAdapter(DUTAdapter):
    """
    Adapter for DMA controllers.

    Expected raw record format:
      {
        "channel":    N,
        "op":         "READ" | "WRITE" | "TRANSFER" | "DONE",
        "src_addr":   "0x...",
        "dst_addr":   "0x...",
        "length":     N,
        "cycles":     N,
        "error":      false | true,
      }
    """
    dut_class = DUTClass.DMA
    name      = "dma_adapter"

    def translate_record(self, raw: Dict, seq: int) -> Dict:
        op  = raw.get("op", "dma_op")
        src = raw.get("src_addr", "0x0")
        dst = raw.get("dst_addr", "0x0")
        rec = self._base_record(seq, pc=seq * 4, disasm=f"dma_{op.lower()}")

        if op in ("READ", "TRANSFER"):
            rec["mem_reads"] = [{"addr": src, "size": raw.get("length", 4), "value": "0x0"}]
        if op in ("WRITE", "TRANSFER"):
            rec["mem_writes"] = [{"addr": dst, "size": raw.get("length", 4), "value": "0x0"}]

        rec["regs"] = {
            "a0": f"0x{raw.get('channel', 0):08x}",
            "a1": src,
            "a2": dst,
        }
        if raw.get("error"):
            rec["trap"] = {"cause": 7, "tval": dst}  # store/AMO access fault
        if raw.get("cycles"):
            rec["perf_counters"] = {"cycles": raw["cycles"], "instret": 1}
        return rec


# ─────────────────────────────────────────────────────────
# UART adapter
# ─────────────────────────────────────────────────────────

class UARTAdapter(DUTAdapter):
    """
    Adapter for UART controllers.

    Expected raw record format:
      {
        "op":        "TX" | "RX" | "CONFIG",
        "data":      "0x.." (one byte),
        "baud_rate": N,
        "parity":    "NONE" | "EVEN" | "ODD",
        "framing_error": false | true,
        "parity_error":  false | true,
        "cycles":    N,
      }
    """
    dut_class = DUTClass.UART
    name      = "uart_adapter"

    def translate_record(self, raw: Dict, seq: int) -> Dict:
        op   = raw.get("op", "uart_op")
        data = raw.get("data", "0x0")
        rec  = self._base_record(seq, pc=seq * 4, disasm=f"uart_{op.lower()}")
        rec["regs"] = {
            "a0": data,
            "a1": f"0x{raw.get('baud_rate', 115200):08x}",
        }
        framing_err = raw.get("framing_error", False)
        parity_err  = raw.get("parity_error", False)
        if framing_err or parity_err:
            cause = 5 if framing_err else 4
            rec["trap"] = {"cause": cause, "tval": "0x0"}
        if raw.get("cycles"):
            rec["perf_counters"] = {"cycles": raw["cycles"], "instret": 1}
        return rec


# ─────────────────────────────────────────────────────────
# Custom (passthrough) adapter
# ─────────────────────────────────────────────────────────

class CustomAdapter(DUTAdapter):
    """
    Passthrough adapter for records already in AVA commit-log format.
    Validates and fills in missing required fields.
    """
    dut_class = DUTClass.CUSTOM
    name      = "custom_adapter"

    def translate_record(self, raw: Dict, seq: int) -> Dict:
        rec = self._base_record(seq)
        rec.update(raw)
        rec["seq"]            = raw.get("seq", seq)
        rec["schema_version"] = SCHEMA_VERSION
        rec.setdefault("regs", {})
        rec.setdefault("csrs", {})
        return rec


# ─────────────────────────────────────────────────────────
# Registry and factory
# ─────────────────────────────────────────────────────────

_ADAPTER_REGISTRY: Dict[DUTClass, Type[DUTAdapter]] = {
    DUTClass.CRYPTO: CryptoAdapter,
    DUTClass.DMA:    DMAAdapter,
    DUTClass.UART:   UARTAdapter,
    DUTClass.CUSTOM: CustomAdapter,
}


def get_adapter(dut_class: DUTClass | str) -> DUTAdapter:
    """
    Factory: return an adapter instance for the given DUT class.

    Parameters
    ----------
    dut_class : DUTClass enum value or string name

    Returns
    -------
    DUTAdapter instance
    """
    if isinstance(dut_class, str):
        dut_class = DUTClass(dut_class.lower())
    cls = _ADAPTER_REGISTRY.get(dut_class, CustomAdapter)
    return cls()


def register_adapter(dut_class: DUTClass, adapter_cls: Type[DUTAdapter]) -> None:
    """Register a custom adapter for a DUT class."""
    _ADAPTER_REGISTRY[dut_class] = adapter_cls
    logger.info("Registered adapter %s for %s", adapter_cls.__name__, dut_class)


# ─────────────────────────────────────────────────────────
# Cross-domain manifest integration
# ─────────────────────────────────────────────────────────

def run_from_manifest(manifest_path: Path) -> int:
    with open(manifest_path) as f:
        manifest = json.load(f)

    dut_class_str = manifest.get("agent_config", {}).get("dut_class", "cpu")
    if dut_class_str.lower() == "cpu":
        logger.info("CrossDomain: CPU DUT, no adapter needed")
        return 0

    run_dir     = Path(manifest["run_dir"])
    outputs     = manifest.get("outputs", {})
    raw_rtl_log = run_dir / (outputs.get("raw_rtl_log") or "raw_rtl.jsonl")
    raw_iss_log = run_dir / (outputs.get("raw_iss_log") or "raw_iss.jsonl")

    if not raw_rtl_log.exists():
        logger.warning("CrossDomain: no raw_rtl_log found, skipping")
        return 0

    adapter  = get_adapter(dut_class_str)
    out_rtl  = run_dir / "rtl_commit.jsonl"
    out_iss  = run_dir / "iss_commit.jsonl"

    n_rtl = adapter.translate_file(raw_rtl_log, out_rtl)
    n_iss = adapter.translate_file(raw_iss_log, out_iss) if raw_iss_log.exists() else 0

    manifest.setdefault("outputs", {}).update({
        "rtl_commit_log": "rtl_commit.jsonl",
        "iss_commit_log": "iss_commit.jsonl",
    })

    report = {
        "schema_version": SCHEMA_VERSION,
        "agent":          "cross_domain",
        "dut_class":      dut_class_str,
        "adapter":        adapter.name,
        "rtl_records":    n_rtl,
        "iss_records":    n_iss,
        "translated_at":  datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    with open(run_dir / "cross_domain_report.json", "w") as f:
        json.dump(report, f, indent=2)
        f.write("\n")

    tmp = manifest_path.with_suffix(".json.tmp")
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    tmp.rename(manifest_path)
    return 0
