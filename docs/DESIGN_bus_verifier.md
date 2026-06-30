# Design & Build Plan — T34 Bus Protocol Verifier (AXI / AHB / APB)

**Status:** Implemented & tested (AVA v2.12.0, 2026-06-30)
**Module:** `AGENT_H/bus_verifier.py`
**Owner agent:** AGENT_H (Extended Verification Research Tier)

---

## 1. Why this

**Levels 4–5**, **priority #7**. On-chip buses (AXI4 dominant, plus AHB/APB) are
the connective tissue of every SoC, and their protocol rules — burst beat
addressing, the 4 KB-boundary prohibition, WRAP alignment, `WLAST` placement —
are arithmetic invariants that are easy to get subtly wrong and hard to catch by
inspection. This verifier pairs naturally with the cache verifier: a write-back
cache's eviction traffic *is* a sequence of bus bursts.

---

## 2. The novel core — a golden AXI burst generator

`axi_expected_beats(addr, length, size, burst)` produces the exact
beat-address sequence the AXI spec mandates:

- **INCR** — `addr + i·2^size`.
- **FIXED** — the same address every beat.
- **WRAP** — increments and wraps at the `(len+1)·2^size` boundary
  (`boundary = addr − addr mod total`), the classic source of off-by-one cache
  refill bugs.

`crosses_4kb()` independently encodes the rule that a burst may never cross a
4 KB page. These are pure arithmetic, so the golden reference is exact and
unit-tested against hand-computed sequences (e.g. WRAP4 from `0x1008` →
`0x1008, 0x100c, 0x1000, 0x1004`).

---

## 3. Checks & metrics

| Check | Severity | Catches |
|---|---|---|
| `bus_burst_length` | HIGH | number of beats ≠ AxLEN + 1 |
| `bus_wlast` | HIGH | `LAST` not on (exactly) the final beat |
| `bus_beat_addr` | HIGH | a beat address ≠ the mandated address (FIXED/INCR/WRAP) |
| `bus_4kb_boundary` | HIGH | an INCR/FIXED burst crosses a 4 KB page |
| `bus_wrap_invalid` | HIGH | WRAP length ∉ {2,4,8,16} or unaligned start |
| `bus_resp` | HIGH | response code invalid for the protocol |

**Metrics** (analytics, never fail the run): transactions, reads, writes, beats,
error responses. Valid response sets are per-protocol (`axi4`: okay/exokay/
slverr/decerr; `ahb`/`apb`: okay/error).

---

## 4. Soundness & gating

- Each check fires only for the descriptor fields a transaction actually
  provides: beat-level checks require an observed `beats` list; burst checks
  require `addr`/`len`/`size`. A transaction with an unknown protocol or missing
  fields is counted in the metrics but never failed.
- Robust against malformed input (non-dict transactions, `None` fields) — every
  transaction is processed under a guard.

### Optional trace contract (additive only)

A transaction is read either from a `bus_trace.jsonl` file or from a commit
record's additive `bus` field (a dict or a list of dicts):

```
{ "protocol":"axi4", "id":0, "txn":"write", "addr":"0x..",
  "len":3, "size":2, "burst":"incr",
  "beats":[{"addr":"0x..","data":"0x..","last":true}, ...],
  "resp":"okay" }
```

---

## 5. Report & integration

`run()` returns the standard schema v2.1.0 report plus `transactions` and
`metrics`, band `CLEAN→CRITICAL`. Wired into `_run_extended_pipeline`
(`_bus` import, `EXTENDED_AGENTS_AVAILABLE`, extracts the `bus` fields from the
commit log, writes `bus_report.json`, records `reports["bus"]`) and exported
from `AGENT_H/__init__.py`. Standalone:
`python AGENT_H/bus_verifier.py --bus bus_trace.jsonl`.

---

## 6. Test coverage

`tests/test_agents.py::TestBusVerifier` — 15 cases: golden FIXED/INCR/WRAP beat
vectors, clean INCR pass with metrics, burst-length bug, beat-address bug,
`WLAST` bug, 4 KB-boundary crossing, invalid WRAP, invalid response code, APB
single transfer, malformed-input robustness, report schema, manifest round-trip
(transactions embedded in the commit log).

**Full suite: 285 passed, 1 skipped**, `compileall` clean, orchestrator
self-test passes.

---

## 7. Limitations / next steps

- **Handshake-level (VALID/READY) checks** — VALID-stability, no VALID-waits-for-
  READY, and outstanding-transaction depth — need a cycle-level signal trace
  rather than transaction descriptors; a future signal-trace input enables them.
- **ID-based ordering / interleaving** — per-ID response ordering and write-data
  interleaving need the channel event order, not bundled transactions.
- **Exclusive access (EXOKAY)** and **QoS/region** semantics are recognised but
  not yet checked.
- **AHB/TileLink/Wishbone** are accepted via the normalised descriptor; native
  field adapters (like the `cross_domain` peripheral adapters) would let raw
  per-protocol traces feed in directly.
