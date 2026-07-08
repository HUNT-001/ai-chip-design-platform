# Design & Build Plan — T48 Debug & Trigger Module Checker

**Status:** Implemented & tested (AVA v2.32.0, 2026-06-30)
**Module:** `AGENT_H/debug_verifier.py`

---

## 1. Why this level

Debug bugs break every debugger and every self-hosted bring-up flow: a hardware
breakpoint that fires on the wrong address (or fails to fire), a halt that
reports the wrong `dcsr.cause`, a single-step that runs two instructions, an
abstract command that reads a stale register. None of this is covered by the
trap/privilege agents (they check the CSR side of an already-delivered trap).
This agent adds the RISC-V **Debug** spec + hardware **Trigger** module level.

## 2. Trigger (mcontrol) — golden match

`Trigger` models an mcontrol trigger (execute/load/store enables, priv set,
`action`, `tdata2`). A trigger **fires** on an access iff:

- the access type is enabled (execute for a fetch, load/store for a data access),
- the current privilege is in the trigger's enabled set, and
- `tdata2` equals the accessed address (execute ⇒ PC, load/store ⇒ data addr).

Checked against the DUT's reported `fired` flag:

| Check | Catches |
|---|---|
| `trigger_missed` | the condition matched but the DUT did not fire |
| `trigger_spurious` | the DUT fired with no matching enabled trigger |
| `trigger_cause` | a fire that enters debug must set `dcsr.cause = 2` |

Privilege-gating and access-type-gating are both exercised in tests (an M-only
trigger must not fire in U; a load trigger must not fire on a store).

## 3. Debug-mode entry & single-step

- **`debug_cause`** — `dcsr.cause` must reflect the source, via the golden map
  `ebreak=1, trigger=2, haltreq=3, step=4, resethaltreq=5`.
- **`debug_dpc`** — `dpc` holds the PC of the halted instruction.
- **`step_count`** — a single-step executes **exactly one** instruction (the
  classic "step ran two" bug).

## 4. Abstract commands

- **`abstract_nothalted`** — an abstract command may only execute while halted;
  a command that produced a result with no error while running is flagged.
- **`abstract_result`** — an access-register read returns the register's true
  value.

## 5. Trace contract (additive, separate stream)

```
debug_trace.jsonl (in order):
  {"op":"trigger_config","index":0,"execute":true,"tdata2":"0x80000040","action":1,"priv":["M"]}
  {"op":"exec","pc":"0x80000040","priv":"M","fired":true,"dcsr_cause":2}
  {"op":"load","addr":"0x2000","pc":"0x80000010","priv":"M","fired":false}
  {"op":"halt","cause":"haltreq","dpc":"0x80000010","dcsr_cause":3}
  {"op":"step","instrs_executed":1}
  {"op":"resume"}
  {"op":"abstract","cmd":"access_reg","regno":10,"halted":true,"result":"0x5","expected":"0x5"}
```

Config is cumulative; `tdata1` may be given as a nested dict or as flat fields.
Clean no-op on an empty / absent trace.

## 6. Integration & tests

Wired into `_run_extended_pipeline` (`_debug`, `run_from_manifest` →
`debug_report.json`). Exported from `AGENT_H/__init__.py`.
`tests/test_agents.py::TestDebugVerifier` — 9 cases (validated standalone: 12):
trigger fire/missed/spurious, non-match + priv gating, load-vs-store type,
trigger cause, debug cause/dpc/step, abstract halted/result, robustness/schema,
manifest. All pass.

> Build note: stdlib-only and self-contained; the workspace mount truncates
> recently-grown files so the full in-repo suite runs against the real repo.
> Additive change, existing agents unaffected.

## 7. Limitations / next steps

- **Trigger match types** beyond equality (`match` 2/3 ≥/< , napot, mask) and
  **chained** triggers.
- **icount / itrigger / etrigger** (instruction-count and interrupt/exception
  triggers) and **tdata timing** ("before" vs "after" the access commits).
- **Debug Module Interface (DMI)** register protocol (dmcontrol/dmstatus,
  abstractauto, program buffer) and **system-bus access**.
- **Multi-hart** halt-group / resume-group semantics.
