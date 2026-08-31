# VSA Kernel — Completeness Verdict, Freeze Decision, and Ecosystem Roadmap

**Decision record.** Applies the kernel-stability rule to the full "Verification
Civilization" vision and records the freeze decision + the phase plan. After
this point, innovation moves *above* the kernel.

## The rule (the test we applied)

> Every new capability must integrate through the VSA kernel **without requiring
> changes to the kernel**. If a capability forces a kernel change, either the
> kernel was incomplete or the capability belongs at a different layer.

## Completeness result

Every layer of the vision and every OS property was mapped to an existing kernel
primitive (full table in the chat record). **No kernel change is required**, and
several OS-grade properties are *consequences of kernel laws*, not additions:

- **Confluence (Struct-1)** ⇒ conflict-free concurrent / transactional updates.
- **Witness + firewall (Wit-1, Fire-1)** ⇒ security: nothing enters the canonical
  state without adjudicated evidence; sign-off authority is the witness gate.
- **Functional purity (`W_{t+1}=Φ(W_t)`) + Struct-2** ⇒ checkpoint / pause /
  resume / async survival of long regressions.
- **Utility (`𝒰`)** ⇒ the economic model (cost × expected value per action).
- **Merge (`K⊔K'`)** ⇒ multi-agent contribution integration; inconsistency
  surfaces as a bug, not silent corruption.
- **Judgments (`E⊢φ:κ`)** ⇒ the typed, evidence-backed messaging content type.
- **Approximation layer (`Ŝ≈S`)** ⇒ per-agent digital twins with bounded
  divergence.

## The one boundary decision (locked)

**VSA models the design-epistemic state; the OS/society models the process.**
The Social / Human / Strategic world models — an agent reasoning about *other
agents and the verification process* — have no canonical referent in VSA and are
**OS-layer** constructs. Inter-agent **trust** is *computed at the OS layer from
kernel-provided provenance* (every judgment owns its witness) plus each source's
calibration history. Making the verification process a first-class VSA object
(meta-verification) would require a kernel extension and is **explicitly declined**
to keep the kernel stable.

**Semantic specialisation is state ownership, not a task role.** A domain expert
= the agent owning a region of the failure σ-algebra (its property class) + the
relevant schemas in its semantic memory `M_s`. "Cache Verification Engineer" owns
the coherence properties + MESI/protocol schemas — expertise as kernel state, not
a microservice.

## Verdict

**VSA v1.0 is FROZEN as the canonical kernel**, under the boundary decision above.
Kernel changes now require re-opening this record with a completeness
counter-example (a capability that cannot plug in). The attack sheet
(`VSA_ATTACK_SHEET.md`) governs any challenge to the kernel's *internal* claims.

---

## Ecosystem roadmap (innovation now lives here, not in the kernel)

Governing invariant for every phase: **plug into the frozen kernel; do not
change it.**

- **Phase 1 — VSA Kernel. ✅ complete.** Canonical state, laws, runtime
  invariants, executable reference (`vsa_reference.py`), validation scripts.
- **Phase 2 — Verification OS.** Event scheduler (attention = `argmax 𝒰` across
  tasks), resource manager (licenses/compute/FPGA as `cost(a)` + budget `C`),
  transactional evidence ledger over the canonical state (safe by Struct-1),
  typed evidence-backed message bus (payload = judgments), agent lifecycle
  (checkpoint/resume via kernel purity), permission/sign-off model (the witness
  gate). *All expressible on the frozen kernel.*
- **Phase 3 — One exceptional autonomous engineer.** Reads RTL + spec, forms
  hypotheses (`Γ`), plans sim/formal (`π` over `𝒰`), learns (`Learn`), improves
  (`M_s/M_p`), operates *entirely through the kernel*. **If one cannot function
  autonomously, ten cannot.** *Gating dependency: real evidence — Verilator +
  Ibex + coverage — the same data/tool integration in
  `DATA_AND_HARDWARE_REQUIREMENTS.md`.*
- **Phase 4 — Specialised engineers.** Cache, pipeline, memory-subsystem,
  protocol (AXI/TileLink/CXL), interrupt/exception, formal, coverage-closure,
  UVM-infra. Specialisation = property-class + `M_s` ownership.
- **Phase 5 — Collaborative organisation.** Leads, conflict resolution (kernel
  merge), knowledge sharing (judgment messages), cross-agent planning, review/
  approval (witness authority).
- **Phase 6 — Self-improving organisation.** New strategies, resource-allocation
  optimisation, reusable methodologies, proposed assertions/abstractions — each
  a `Γ`/`M_p` extension, each validated before adoption.
- **Phase 7 — Research organisation.** Genuinely new verification techniques,
  evaluated experimentally (the falsifiable-criterion discipline) before
  proposal. New techniques enter as new `Ω` evidence channels — still no kernel
  change.

## Discipline (carried from the user's caution)

1. Build **one** exceptional engineer before many (Phase 3 before Phase 4+).
2. Every capability integrates through the kernel unchanged, or it is rejected or
   re-levelled.
3. Real autonomy needs real evidence: Phase 3 is gated on the Verilator/data
   integration, not on more architecture.
4. The kernel stays about the **design**; the ecosystem is about the **process**.
