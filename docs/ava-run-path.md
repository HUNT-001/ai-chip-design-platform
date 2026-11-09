# The path of a run

Where a run's inputs go and where its exit code comes from. Boxes are placed by
their call site in `ava_patched.py`, not by the agent directory names: the
orchestrator runs phases 1 to 5 with classes defined in its own file, and imports
AGENT_B, F, G, H, I, J, K and L. It does not import AGENT_A, C, D or E, which are
standalone CLIs. Its `CommitLogComparator` is its own copy of AGENT_D's.

```mermaid
flowchart TD
    IN["Inputs<br/>RTL file · seed"] --> CLI["ava_patched.py main()"]
    CLI --> P1

    subgraph P1 ["Phase 1 · semantic analysis"]
        SA["AVA._semantic_analysis<br/>(in ava_patched.py)"]
    end
    P1 --> P2

    subgraph P2 ["Phase 2 · testbench generation"]
        TB["AVA._generate_tb_suite<br/>RV32IMTestGenerator (in ava_patched.py)"]
    end
    P2 --> P3

    subgraph P3 ["Phase 3 · tandem simulation (SpikeISS.run_tandem)"]
        RTL["RTL sim<br/>Verilator subprocess"]
        ISS["ISS<br/>Spike subprocess"]
        COV["AGENT_F coverage_pipeline<br/>coverage backend"]
        CMP["CommitLogComparator<br/>(in ava_patched.py)"]
        RTL --> CMP
        ISS --> CMP
        RTL --> COV
    end
    RTL -->|"no commit log<br/>(tool missing or stubbed)"| UNOBS(["unobserved"])
    ISS -->|"no commit log<br/>(tool missing or stubbed)"| UNOBS
    CMP -->|"any mismatch"| BUGS(["results.bugs<br/>comparator:KIND"])
    P3 --> P4

    subgraph P4 ["Phase 4 · analysis"]
        AN["AVA._performance_cop<br/>AVA._security_injector"]
    end
    P4 --> P5

    subgraph P5 ["Phase 5 · coverage adaptation"]
        CA["UCB1CoverageDirector.adapt_cold_paths<br/>(in ava_patched.py)"]
    end
    P5 --> P6

    subgraph P6 ["Phase 6 · extended verification (optional: only if self.enable_extended)"]
        EXT["_run_extended_pipeline<br/>AGENT_I memory model · AGENT_J CDC<br/>AGENT_K perf · AGENT_L equivalence<br/>AGENT_H verifiers · AGENT_G causal engine<br/>AGENT_B testbench_generator"]
    end

    EXT -->|"verifier exits 1"| VIOL(["violations"])
    EXT -->|"verifier reports pass:false"| VIOL
    EXT -->|"verifier exits 3, or a stage hits an<br/>OSError / timeout (run_stage)"| INC(["incomplete list"])
    UNOBS --> INC
    P5 -.->|"extended off"| V

    BUGS --> V
    VIOL --> V
    INC --> V
    V{{"verdict_from_reports"}}

    V -->|"any finding<br/>(a finding outranks could-not-check)"| F(["failed · exit 1"])
    V -->|"no finding, something could not verify"| I(["incomplete · exit 3"])
    V -->|"no finding, nothing missing"| C(["completed · exit 0"])

    style UNOBS fill:#3a2d1d,stroke:#d29922,color:#fff
    style INC fill:#3a2d1d,stroke:#d29922,color:#fff
    style I fill:#3a2d1d,stroke:#d29922,color:#fff
    style BUGS fill:#3a1d1d,stroke:#f85149,color:#fff
    style VIOL fill:#3a1d1d,stroke:#f85149,color:#fff
    style F fill:#3a1d1d,stroke:#f85149,color:#fff
    style C fill:#0d2818,stroke:#3fb950,color:#fff
```

- **`failed` / 1** comes from two places only: a comparator finding in
  `results.bugs`, or a verifier that exits 1 or reports `pass: false`.
- **A missing tool becomes `incomplete` / 3** in two ways. A simulator that
  produced no commit log is recorded as `unobserved` (#138), because nothing was
  compared. A stage that raises an environment error is recorded by `run_stage`
  (#10). Both feed the same list, and an empty list is the only way to `completed`.
- The exit code is the verdict and does not depend on the console encoding (#147).
- VOE is not drawn: the observer that would connect it to this path is a spec
  only (`voe/ava-observer`, local, no adapter code), so it is not part of a run.
