# AVA → VOE observer, stage 1: observe-only

`voe/ava_observer.py` reads one finished AVA run and appends one record of what that run
observed to `voe_bench/ava_observations.jsonl`. It makes no trust claim: no promotion, no score,
no MET / NOT MET, and it never calls `R`, `KnowledgeState` or `check_laws`. It only reads.
Nothing in AVA imports it, and nothing it produces goes back into AVA's verdict, status or exit code.

**Kernel (frozen; loaded unmodified, the `phase3/engineer.py:35` way).** `Judgment(phi, warrant,
evidence, witness)` at `docs/vsa_reference.py:13`: it raises `ValueError` (Wit-1) if `witness is
None` and imposes nothing else. `Warrant` at `:8` is `DEFINITIONAL|DEDUCTIVE|INDUCTIVE`.
`KnowledgeState` at `:19`: `K: phi -> Judgment`. `disproven()` reads
`evidence["counterexample"]`, `proven()` means `warrant == DEDUCTIVE`, and `n_eff()` reads
`evidence["n_eff"]` with a default of 0. `check_laws(ks, props, prev_R, event)` at `:71` returns
`(R, [(law, bool)])`. Stage 1 constructs `Judgment`s and nothing else.

## Inputs (as written today, `7209e82`)

- `run_manifest.json` (`ava_patched.py:2196`, finalised at `:3438`): `schema_version`, `run_id`,
  `run_dir`, `status` (= `verdict_from_reports(...)`; it stays `running_extended` if the final
  update never ran), `started_at`, `finished_at`, `isa`, `dut_module`, `rtl_sources`,
  `metrics{total_commits, total_mismatches, tests_run, extended_reports}`, `outputs{}`,
  `agent_config{}`, plus `phases{<x>_check: {status, violations}}` written back by the verifiers
  (24 names in `AGENT_*`). **The manifest carries only the folded `status`.** The per-channel
  evidence is not in it.
- `ava_results_<mod>_<ts>.json` in the same `run_dir` (`:3528`): `status`, `industrial_grade`,
  `initial_results.{bugs, coverage, metadata.{coverage_source, coverage_measured,
  rtl_instructions, iss_instructions}}`, and `extended_reports{<key>: ...}`, including
  `_verdict{violations, incomplete}`. If there are zero results files, every channel that
  depends on them is UNOBSERVED. If there is more than one, the observer raises.

## (a) Channel → Judgment mapping

`phi` is a property's identity and is the same across runs. `witness` is
`<file>#<json-pointer>@sha256:<first 12 hex>` and is never empty.

| channel | source | class | n |
|---|---|---|---|
| `comparator` | `initial_results.bugs` | comparator | 1 |
| `industrial_grade` | `industrial_grade` + coverage metadata | grade | 1 |
| `report/<key>` | `extended_reports[key].pass` (intent…rtl_graph, plus the 10 `:2820` loop keys) | verdict | 50 |
| `exit/<key>` | `extended_reports[key].exit_code`: `agent_i/j/k/l`, `peripheral` (via `_call`) | exit | 5 |
| `report/{dashboard,testbench_gen}` | `"pass": True` as a literal (`:3383`, `:3434`) | constant | 2 |
| `report/verification_twin` | in `NON_GATING_REPORT_KEYS` (`:269`) | advisory | 1 |
| `report/<key>` | no `pass`: fault_injection, security_intel, root_cause, causal_tests, knowledge_graph, explanation, economics, confidence, coverage, self_evolving, stimulus | advisory | 11 |

- OBSERVED-FAIL → `Judgment(phi, INDUCTIVE, {"counterexample": True, ...detail}, witness)`.
- OBSERVED-PASS → `Judgment(phi, INDUCTIVE, {...detail}, witness)`. It carries no `n_eff` (see Q1).
- UNOBSERVED → **no Judgment.** The record gets `judgment: null` and a `reason`.
  `DEDUCTIVE` and `DEFINITIONAL` are never used in stage 1 (Q2).

## (b) Three-state rules. The checks run in order and the first match wins.

1. **comparator.** FAIL if any bug is present (every severity counts, mirroring
   `ADVISORY_BUG_SEVERITIES = ∅`, `:308`). PASS only if `bugs == []` **and**
   `rtl_instructions > 0` **and** `iss_instructions > 0`. Otherwise UNOBSERVED. An empty bug
   list from two empty logs means the comparator saw nothing.
2. **grade.** UNOBSERVED unless `coverage_measured is True` (the key is absent when `coverage` is
   empty, `:524`) and `coverage_source` is not one of `legacy_dict`, `NO_DATA`, `fallback_dict`
   or `""`. Otherwise FAIL if `industrial_grade` is False and PASS if it is True.
3. **verdict.** UNOBSERVED if the key is absent or skipped, if the entry has `status: "error"`,
   or if the stage's label is in `_verdict.incomplete` (exit 3). FAIL if `pass is False` or the
   stage's label is in `_verdict.violations`. PASS only if `pass is True` **and** the raw
   `<x>_report.json` in `run_dir` contains a literal `"pass": true`. Otherwise UNOBSERVED with
   `pass_defaulted`, because the orchestrator writes `r.get("pass", True)` (Q7).
4. **exit.** Absent, `status: "error"`, or `exit_code == 3` → UNOBSERVED, whatever `status` says.
   `exit_code == 0` → PASS. Any other int → FAIL. An entry with no int `exit_code` raises.
5. **constant / advisory.** Always UNOBSERVED (`no_verdict`). Whether the stage ran is recorded
   separately. A field that cannot be False was not observed.

The run-level `status` from the manifest and from the results file is recorded verbatim and does
not set any channel's state. `state_counts` is a tally of the three states, not a score.

## (c) Unknown means stop

The observer keeps four committed registries: the `extended_reports` keys (69 plus `_verdict`),
the `_verdict` labels (mapped to keys by hand: `memmodel`→`memory_model`, `hyp`→`hypervisor`,
`CSR verifier`→`csr`, …), the `phases` names, and the top-level manifest keys. Anything outside
them raises `UnknownChannelError` and nothing is appended. A test AST-scans `ava_patched.py` and `AGENT_*` and asserts that each registry covers what the
source can emit, so a channel AVA adds tomorrow fails CI the day it lands. `outputs{}` holds file
pointers, not verdicts, and is recorded without a registry.

## (d) Record schema (one JSON object per line, append-only)

```
{"schema": "ava_observation/1", "observed_at": ISO-8601 UTC,
 "observer": {"path", "sha256"}, "kernel": {"path": "docs/vsa_reference.py", "sha256"},
 "ava": {"git_sha", "git_dirty", "sha_source": "observer_checkout"},
 "manifest": {"path", "sha256", "run_id", "status"}, "results": {"path", "sha256", "status"} | null,
 "channels": {"<channel>": {"class", "state": "OBSERVED-FAIL|OBSERVED-PASS|UNOBSERVED", "reason",
              "evidence": {...}, "judgment": {"phi", "warrant", "evidence", "witness"} | null}},
 "phases": {verbatim}, "contradictions": [{"channel", "a", "b"}], "state_counts": {...}}
```

The observer opens the file only in `"a"` mode, writes one line, then calls `fsync`. It refuses
any `--out` that resolves to `capability_ledger.json`. The default output is
`voe_bench/ava_observations.jsonl`, and the tests use `tmp_path`.

## (e) Open questions: what the kernel can't represent, and decisions for you

- **Q1. Weight.** A pass's `n_eff` decides `R`, which is a trust claim. Stage 1 omits it, and
  `n_eff()` reads the missing key as 0. So to the kernel, an OBSERVED-PASS weighs the same as an
  UNOBSERVED channel. The kernel can express "unobserved" only as absence from `K`, so the
  PASS/UNOBSERVED distinction lives in the record, not the kernel.
- **Q2. No DEDUCTIVE.** Agent L is a depth-10 bounded check and `run_formal` is False (`:2217`).
  Treating either as `proven()` would remove the channel from `R`.
- **Q3. Severity.** `counterexample` is a bool; bug kind, count and severity live in `evidence` only.
- **Q4. Wit-1 checks only `witness is not None`,** so it accepts `""`. The observer rejects empty.
- **Q5. Loading the kernel runs its demo and calls `random.seed(2026)`** (resets the global RNG).
  `phase3/engineer.py:36` says "WITHOUT running its demo"; it only silences stdout. Harmless
  here, and not fixable without editing the kernel.
- **Q6. No AVA output records AVA's SHA.** The observer records its own checkout's SHA, a proxy.
- **Q7. Defaulted passes.** Rule 3 needs a key → raw-report table. I haven't measured how many of
  the 50 write a raw report; any that don't will read UNOBSERVED in every run.
- **Q8. AVA contradicts itself; recorded, not resolved.** `_call` labels exit 3 `"violation"`
  (`:2284`) but files it under `incomplete`. In-process CSR with `pass` False and 0 violations
  folds as FAIL; its CLI returns 3 (`AGENT_H/csr_verifier.py:560`). `phases` and `reports` are
  two copies of one verdict. Candidate issues, not filed.
- **Q9.** Is industrial grade NO an OBSERVED-FAIL, or no judgment at all? The spec says FAIL.
