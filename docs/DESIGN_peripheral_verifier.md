# Design & Build Plan — T24 SoC Peripheral Protocol Verifier

**Status:** Implemented & tested (AVA v2.2.0, 2026-06-26)
**Module:** `AGENT_H/peripheral_verifier.py`
**Owner agent:** AGENT_H (Extended Verification Research Tier)

---

## 1. Why this

The roadmap shows DMA / UART / CRYPTO SoC integration as **partially addressed**.
What existed (`AGENT_H/cross_domain.py`) only *translated* a peripheral DUT's
output into the AVA commit-log schema — a format shim. Nothing *verified* that
the peripheral obeyed its protocol contract. This module closes that gap by
adding a golden reference model + scoreboard per domain, reusing the same
`run()` / `run_from_manifest()` pattern as the rest of AGENT_H and requiring no
EDA tools.

`cross_domain.py` (translate) and `peripheral_verifier.py` (verify) are
complementary: the adapter produces the canonical commit log for the CPU-style
agents, while the verifier checks the raw protocol stream directly.

---

## 2. Architecture

```
   raw DUT log (raw_rtl.jsonl, DUT-specific format)
                     │
        PeripheralVerifier(records, dut_class)
                     │  get_checker(dut_class)
        ┌────────────┼─────────────┐
        ▼            ▼             ▼
   DMAChecker   UARTChecker   CryptoChecker
   (FSM +        (config FSM   (KAT + round-trip
    byte SB)      + integrity)  + leak/determinism)
        └────────────┼─────────────┘
                     ▼
            PeripheralViolation[]
                     ▼
       report{ band, pass, violations, stats }
```

Each checker is a stateful `PeripheralChecker` with `check_record(raw, seq)`
(per record) and `finalize()` (end-of-stream, e.g. dangling DMA channels).
A factory/registry (`get_checker`, `register_checker`) mirrors `cross_domain`'s
adapter registry, so new peripherals plug in the same way.

---

## 3. Checks per domain

### DMA — channel FSM + byte-conservation scoreboard

| Check | Severity | Catches |
|---|---|---|
| `dma_byte_mismatch` | HIGH | DONE with read-bytes ≠ write-bytes (lost/dup data) |
| `dma_write_underflow` | HIGH | wrote more bytes than were read |
| `dma_null_src` / `dma_null_dst` | HIGH | transfer to/from a null pointer |
| `dma_bad_length` | HIGH | non-positive transfer length |
| `dma_use_after_error` | HIGH | new op on a channel still in the error state |
| `dma_overlap` | MEDIUM | TRANSFER with overlapping src/dst regions |
| `dma_spurious_done` | MEDIUM | DONE with no prior activity |
| `dma_dangling_channel` | MEDIUM | channel left active without DONE (finalize) |

### UART — configure-before-use + frame integrity

| Check | Severity | Catches |
|---|---|---|
| `uart_data_overflow` | HIGH | data byte outside an 8-bit frame |
| `uart_inconsistent_parity_error` | HIGH | parity_error asserted while parity = NONE |
| `uart_bad_baud` | HIGH | non-positive baud rate |
| `uart_unconfigured_use` | MEDIUM | TX/RX before any CONFIG |
| `uart_bad_parity` | MEDIUM | unknown parity mode |

### CRYPTO — reference model + scoreboards

| Check | Severity | Catches |
|---|---|---|
| `sha256_kat` | HIGH | digest ≠ golden `hashlib.sha256` reference |
| `aes_roundtrip` | HIGH | decrypt(encrypt(x)) ≠ x for the same key |
| `crypto_nondeterministic` | HIGH | identical (op,key,input) → different output |
| `crypto_no_key` | HIGH | keyed op (AES/RSA) issued without a key |
| `crypto_error_with_output` | HIGH | ERROR status still exposes a result (leak) |
| `crypto_no_output` | MEDIUM | DONE status with no output |

The SHA-256 check is a true **known-answer test**: the verifier recomputes the
digest from `data_in` with the standard library and compares. AES uses a
*scoreboard* round-trip (encrypt then decrypt under the same key must recover
the plaintext) so no AES implementation or key material is needed.

---

## 4. Severity band & report

Identical contract to every AGENT_H module: `run()` returns
`schema_version, agent, dut_class, records_checked, stats, total_violations,
severity_score, band, pass, violations[]` plus timestamps. Band:
`CLEAN → MINOR → DEGRADED → CRITICAL` (any HIGH violation ⇒ CRITICAL). For
DUT classes without a checker (`cpu`, `custom`) it returns
`status: "skipped", pass: true`.

---

## 5. Integration

- `AGENT_H/__init__.py` exports `PeripheralVerifier`, `get_checker`,
  `register_checker`.
- `ava_patched.py`: `_peripheral` lazy import, added to
  `EXTENDED_AGENTS_AVAILABLE`, and called in `_run_extended_pipeline` via
  `run_from_manifest(mpath)` (it self-gates on `agent_config.dut_class`, so the
  call is a no-op for CPU runs). Writes `peripheral_report.json` and records
  `reports["peripheral"]`.
- Standalone CLI: `python AGENT_H/peripheral_verifier.py --raw log.jsonl
  --dut-class dma`.

---

## 6. Test coverage

`tests/test_agents.py::TestPeripheralVerifier` — 15 cases: factory; DMA clean /
byte-mismatch / null+dangling; UART clean / unconfigured+overflow / parity
inconsistency; CRYPTO SHA-256 KAT pass & fail / error-leak+no-key / AES
round-trip; report schema; CPU skip; manifest round-trip.

**Full suite: 73 passed**, `compileall` clean.

---

## 7. Limitations / next steps

- DMA byte-conservation assumes a READ/WRITE/TRANSFER+DONE descriptor model;
  descriptor-chained / scatter-gather DMA needs a descriptor-list extension.
- AES/RSA correctness is verified by *consistency* (round-trip, determinism),
  not against a cipher KAT — add a vetted AES KAT vector set when key material
  is exposed in the log.
- UART loopback (TX→RX byte pairing) is currently structural; add an ordered
  FIFO scoreboard when a link-id field is available.
- Promote `cross_domain.py` to emit a `raw_rtl_log` output key so the verifier
  and the adapter share one source file in production runs.
