"""
AGENT_H.soc_peripheral_verifier — GPIO / SPI / I²C / Timer / PWM (T68, level 19)
================================================================================

Golden protocol checkers for the SoC peripherals not already covered by
`peripheral_verifier` (which handles DMA / UART / CRYPTO). Together they close
taxonomy **level 19 (Complete SoC)** on the peripheral axis.

GPIO
----
- **gpio_direction** (HIGH) — a pin driven while configured as an input, or read
  as an output-only pin driving a conflicting value.
- **gpio_readback** (HIGH) — reading an output pin returned a value other than
  what was driven (with no open-drain/external-drive declaration).
- **gpio_interrupt** (HIGH) — an edge/level interrupt fired without the
  configured condition occurring, or a configured edge produced no interrupt.

SPI
---
- **spi_mode** (HIGH) — data did not change on the correct clock edge for the
  configured CPOL/CPHA mode (sampling edge = leading for CPHA=0, trailing for
  CPHA=1; idle level = CPOL).
- **spi_cs_protocol** (HIGH) — clock toggled or data transferred while chip
  select was inactive, or CS de-asserted mid-word.
- **spi_bit_order** (HIGH) — the assembled word disagrees with the declared
  MSB/LSB-first bit order.

I²C
---
- **i2c_protocol** (HIGH) — malformed frame: data changed while SCL was high
  (outside START/STOP), missing START before data, or STOP without START.
- **i2c_ack** (HIGH) — no ACK/NACK bit after a byte, or a write to a
  non-responding address reported ACK.
- **i2c_arbitration** (HIGH) — a master that lost arbitration kept driving.

Timer / PWM
-----------
- **timer_period** (HIGH) — the counter's tick/reload period does not match the
  programmed value.
- **timer_overflow** (HIGH) — the counter wrapped without the overflow/interrupt
  flag being set, or set it early.
- **pwm_duty** (HIGH) — the measured high-time / period ratio does not match the
  programmed duty within tolerance.
- **pwm_period** (HIGH) — the measured PWM period does not match the programmed
  period.

Trace contract — `soc_periph_trace.jsonl` (additive; skipped when absent)
-------------------------------------------------------------------------
```
{"event":"gpio","pin":3,"dir":"out","drive":1,"read":1}
{"event":"spi_cfg","name":"s0","cpol":0,"cpha":0,"bits":8,"msb_first":true}
{"event":"spi","name":"s0","cs":true,"sclk":0,"mosi":1,"edge":"rise","bit":0}
{"event":"spi_word","name":"s0","bits":[1,0,1,1,0,0,1,0],"word":"0xb2"}
{"event":"i2c","phase":"start"}
{"event":"i2c","phase":"data","scl":1,"sda_changed":true}
{"event":"i2c","phase":"ack","addr":"0x50","ack":true,"responding":true}
{"event":"timer","name":"t0","period":100,"count":100,"overflow":true}
{"event":"pwm","name":"p0","period":100,"duty":25,"high_time":25}
```

Stdlib-only, schema-v2.1.0, graceful degradation.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

log = logging.getLogger("AGENT_H.soc_periph")

SCHEMA_VERSION = "2.1.0"
AGENT_NAME = "soc_peripheral_verifier"
PWM_TOLERANCE = 0.02          # 2% duty/period tolerance by default


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
        return v.strip().lower() in ("1", "true", "yes", "on", "active")
    return default


class SoCPeripheralVerifier:
    def __init__(self, events: Sequence[Dict[str, Any]]):
        self.events = [e for e in (events or []) if isinstance(e, dict)]
        self.violations: List[Dict[str, Any]] = []
        self.spi_cfg: Dict[str, Dict[str, Any]] = {}
        self.spi_state: Dict[str, Dict[str, Any]] = {}
        self.i2c_started = False
        self.i2c_addressed = False
        self.metrics: Dict[str, Any] = {
            "gpio_ops": 0, "spi_ops": 0, "i2c_ops": 0, "timer_ops": 0,
            "pwm_ops": 0, "checked": 0, "periph_active": False, "by_kind": {},
        }

    def _v(self, seq: Any, check: str, detail: str,
           severity: str = "HIGH") -> None:
        self.violations.append({"seq": seq, "check": check,
                                "severity": severity, "detail": detail})

    def _bump(self, kind: str) -> None:
        self.metrics["periph_active"] = True
        self.metrics["by_kind"][kind] = self.metrics["by_kind"].get(kind, 0) + 1
        self.metrics["checked"] += 1

    # ── main ───────────────────────────────────────────────────────────────
    def run(self) -> Dict[str, Any]:
        started = _now()
        for e in self.events:
            kind = str(e.get("event", "")).lower()
            seq = e.get("seq")
            if kind == "gpio":
                self._gpio(e, seq)
            elif kind == "spi_cfg":
                self.spi_cfg[str(e.get("name", "spi"))] = dict(e)
            elif kind == "spi":
                self._spi(e, seq)
            elif kind == "spi_word":
                self._spi_word(e, seq)
            elif kind == "i2c":
                self._i2c(e, seq)
            elif kind == "timer":
                self._timer(e, seq)
            elif kind == "pwm":
                self._pwm(e, seq)
        return self._report(started)

    # ── GPIO ───────────────────────────────────────────────────────────────
    def _gpio(self, e: Dict[str, Any], seq: Any) -> None:
        self.metrics["gpio_ops"] += 1
        self._bump("gpio")
        pin = e.get("pin")
        direction = str(e.get("dir", "")).lower()
        drive = _to_int(e.get("drive"))
        read = _to_int(e.get("read"))
        if direction in ("in", "input") and drive is not None:
            self._v(seq, "gpio_direction",
                    f"GPIO pin {pin}: driving value {drive} while configured as "
                    f"an input")
        if (direction in ("out", "output") and drive is not None
                and read is not None and read != drive
                and not _truthy(e.get("open_drain"))
                and not _truthy(e.get("external_drive"))):
            self._v(seq, "gpio_readback",
                    f"GPIO pin {pin}: driving {drive} but read back {read}")
        # interrupt condition
        if "irq" in e and "edge" in e:
            edge = str(e.get("edge", "")).lower()
            cfg = str(e.get("irq_cfg", edge)).lower()
            fired = _truthy(e.get("irq"))
            expect = (cfg == "both" and edge in ("rise", "fall")) or (cfg == edge)
            if fired and not expect:
                self._v(seq, "gpio_interrupt",
                        f"GPIO pin {pin}: interrupt fired on '{edge}' but is "
                        f"configured for '{cfg}'")
            elif expect and not fired:
                self._v(seq, "gpio_interrupt",
                        f"GPIO pin {pin}: '{edge}' edge matched config '{cfg}' "
                        f"but no interrupt was raised")

    # ── SPI ────────────────────────────────────────────────────────────────
    def _spi(self, e: Dict[str, Any], seq: Any) -> None:
        self.metrics["spi_ops"] += 1
        self._bump("spi")
        name = str(e.get("name", "spi"))
        cfg = self.spi_cfg.get(name, {})
        cpol = _to_int(cfg.get("cpol")) or 0
        cpha = _to_int(cfg.get("cpha")) or 0
        cs = _truthy(e.get("cs"), True)
        st = self.spi_state.setdefault(name, {"cs": False, "bits": 0})
        # CS protocol
        if not cs:
            if e.get("edge") or _truthy(e.get("active")):
                self._v(seq, "spi_cs_protocol",
                        f"SPI '{name}': clock/data activity while CS inactive")
            if st["cs"] and 0 < st["bits"] < (_to_int(cfg.get("bits")) or 8):
                self._v(seq, "spi_cs_protocol",
                        f"SPI '{name}': CS de-asserted mid-word after "
                        f"{st['bits']} bits")
            st["bits"] = 0
        st["cs"] = cs
        # sampling edge: CPHA=0 samples on the leading edge, CPHA=1 on trailing.
        edge = str(e.get("edge", "")).lower()
        sampled = _truthy(e.get("sampled"), False)
        if cs and edge in ("rise", "fall") and sampled:
            leading = "rise" if cpol == 0 else "fall"
            expect = leading if cpha == 0 else ("fall" if cpol == 0 else "rise")
            if edge != expect:
                self._v(seq, "spi_mode",
                        f"SPI '{name}' mode {cpol}{cpha}: sampled on '{edge}' "
                        f"edge, expected '{expect}'")
            st["bits"] = st.get("bits", 0) + 1
        # idle clock level must equal CPOL when CS is inactive
        sclk = _to_int(e.get("sclk"))
        if not cs and sclk is not None and sclk != cpol:
            self._v(seq, "spi_mode",
                    f"SPI '{name}': idle clock level {sclk} != CPOL {cpol}")

    def _spi_word(self, e: Dict[str, Any], seq: Any) -> None:
        self.metrics["spi_ops"] += 1
        self._bump("spi")
        name = str(e.get("name", "spi"))
        cfg = self.spi_cfg.get(name, {})
        bits = e.get("bits")
        word = _to_int(e.get("word"))
        if not isinstance(bits, (list, tuple)) or word is None:
            return
        msb_first = _truthy(cfg.get("msb_first"), True)
        acc = 0
        seqbits = list(bits) if msb_first else list(reversed(list(bits)))
        for b in seqbits:
            acc = (acc << 1) | (1 if _truthy(b) else 0)
        if acc != word:
            order = "MSB-first" if msb_first else "LSB-first"
            self._v(seq, "spi_bit_order",
                    f"SPI '{name}': bits assembled {order} give 0x{acc:x}, "
                    f"but the reported word is 0x{word:x}")

    # ── I²C ────────────────────────────────────────────────────────────────
    def _i2c(self, e: Dict[str, Any], seq: Any) -> None:
        self.metrics["i2c_ops"] += 1
        self._bump("i2c")
        phase = str(e.get("phase", "")).lower()
        if phase == "start":
            self.i2c_started = True
            return
        if phase == "stop":
            if not self.i2c_started:
                self._v(seq, "i2c_protocol", "STOP condition without a START")
            self.i2c_started = False
            self.i2c_addressed = False
            return
        if phase in ("data", "addr"):
            if not self.i2c_started:
                self._v(seq, "i2c_protocol",
                        f"{phase} phase before any START condition")
            # SDA may only change while SCL is low (outside START/STOP)
            if _to_int(e.get("scl")) == 1 and _truthy(e.get("sda_changed")):
                self._v(seq, "i2c_protocol",
                        "SDA changed while SCL was high (illegal outside "
                        "START/STOP)")
            if phase == "addr":
                self.i2c_addressed = True
            return
        if phase == "ack":
            if "ack" not in e:
                self._v(seq, "i2c_ack", "byte transferred with no ACK/NACK bit")
                return
            ack = _truthy(e.get("ack"))
            responding = e.get("responding")
            if responding is not None and ack and not _truthy(responding):
                self._v(seq, "i2c_ack",
                        f"address {e.get('addr')} ACKed but no device is "
                        f"responding at that address")
            return
        if phase == "arbitration":
            if _truthy(e.get("lost")) and _truthy(e.get("still_driving")):
                self._v(seq, "i2c_arbitration",
                        f"master {e.get('master')} lost arbitration but kept "
                        f"driving the bus")

    # ── Timer ──────────────────────────────────────────────────────────────
    def _timer(self, e: Dict[str, Any], seq: Any) -> None:
        self.metrics["timer_ops"] += 1
        self._bump("timer")
        name = e.get("name", "timer")
        period = _to_int(e.get("period"))
        count = _to_int(e.get("count"))
        elapsed = _to_int(e.get("elapsed"))
        if period and elapsed is not None and elapsed != period:
            self._v(seq, "timer_period",
                    f"timer '{name}': measured period {elapsed} != programmed "
                    f"{period}")
        if period is not None and count is not None and "overflow" in e:
            ovf = _truthy(e.get("overflow"))
            should = count >= period
            if should and not ovf:
                self._v(seq, "timer_overflow",
                        f"timer '{name}': count {count} reached period {period} "
                        f"but no overflow flag was set")
            elif ovf and not should:
                self._v(seq, "timer_overflow",
                        f"timer '{name}': overflow flagged at count {count}, "
                        f"before the period {period}")

    # ── PWM ────────────────────────────────────────────────────────────────
    def _pwm(self, e: Dict[str, Any], seq: Any) -> None:
        self.metrics["pwm_ops"] += 1
        self._bump("pwm")
        name = e.get("name", "pwm")
        period = _to_int(e.get("period"))
        duty = _to_int(e.get("duty"))
        high = _to_int(e.get("high_time"))
        meas_p = _to_int(e.get("measured_period"))
        tol = e.get("tolerance")
        tol = float(tol) if isinstance(tol, (int, float)) else PWM_TOLERANCE
        if period and meas_p is not None:
            if abs(meas_p - period) > max(1.0, period * tol):
                self._v(seq, "pwm_period",
                        f"PWM '{name}': measured period {meas_p} != programmed "
                        f"{period}")
        if period and duty is not None and high is not None:
            want = duty / period if period else 0.0
            got = high / period if period else 0.0
            if abs(want - got) > tol:
                self._v(seq, "pwm_duty",
                        f"PWM '{name}': duty {got:.1%} (high {high}/{period}) "
                        f"!= programmed {want:.1%}")

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
            "periph_active": self.metrics["periph_active"],
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
    name = (manifest.get("outputs", {}) or {}).get("soc_periph_trace",
                                                    "soc_periph_trace.jsonl")
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
        log.warning("soc_peripheral_verifier: cannot read manifest: %s", exc)
        return 0
    run_dir = Path(manifest.get("run_dir", mp.parent))
    events = _load_events(run_dir, manifest)
    if not events:
        rep = {"schema_version": SCHEMA_VERSION, "agent": AGENT_NAME,
               "status": "skipped", "reason": "no soc_periph_trace", "pass": True}
    else:
        rep = SoCPeripheralVerifier(events).run()
        rep["status"] = "completed"
    try:
        (run_dir / "soc_periph_report.json").write_text(
            json.dumps(rep, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("soc_peripheral_verifier: cannot write report: %s", exc)
    return 0 if rep.get("pass", True) else 1


if __name__ == "__main__":  # pragma: no cover
    import argparse
    ap = argparse.ArgumentParser(description="AVA SoC peripheral checker")
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args()
    raise SystemExit(run_from_manifest(args.manifest))
