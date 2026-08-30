# AI Capital Architecture Pattern Register

**Document class:** Living architecture pattern register  
**Programme:** AI Capital  
**Register version:** 0.1  
**Status:** Non-executing architecture companion  
**Prepared:** 2026-08-30  
**Constitutional authority:** `ARCHITECTURE_CONSTITUTION.md`  
**Companion roadmaps:** `AI_CAPITAL_AUTONOMOUS_WORK_KERNEL_ROADMAP.md`, `AI_CAPITAL_MASTER_ROADMAP.md`

---

# 1. Governing rule

This register preserves, characterizes, compares, accepts, defers, rejects, supersedes, and verifies reusable architecture patterns for AI Capital.

It is not itself a backlog, release gate, task tracker, runtime policy, or source of execution authority.

> **Observation is not adoption. Adoption is not implementation. Implementation is not verification.**

Repository-facing entries do not name external products, projects, repositories, branded abstractions, or external pattern identifiers. Any admitted pattern must stand independently under AI Capital vocabulary, invariants, Evidence, failure semantics, and decision authority.

---

# 2. Why this register exists

AI Capital develops through architecture research, implementation learning, incidents, experiments, and programme decisions. Without durable architecture memory, useful ideas either disappear or silently become scope.

The register prevents architecture-by-analogy, vocabulary contamination, idea-to-roadmap collapse, shadow acceptance growth, lost deferrals, Authority drift, infrastructure inflation, acceptance without proof, and research hypotheses silently entering production.

---

# 3. Internal traceability model

Every maintained APR entry separates:

1. **Pattern definition** — vendor-neutral problem and design statement.
2. **Architectural basis** — internal basis such as architecture synthesis, programme decision, implementation learning, incident learning, empirical evaluation, or research hypothesis.
3. **AI Capital interpretation** — adaptation under constitutional boundaries.
4. **Adoption trigger** — concrete forcing function required for promotion.
5. **Required Evidence** — executable or empirical proof required.
6. **Decision log** — AI Capital decision only.

```text
research / implementation / incident / programme need
                      ↓
             architecture synthesis
                      ↓
           vendor-neutral pattern
                      ↓
           AI Capital interpretation
                      ↓
               AI Capital decision
                      ↓
      explicit roadmap / decision / issue link
                      ↓
             implementation + proof
```

---

# 4. Status model

## Pattern status
`OBSERVED` | `CHARACTERIZED` | `CANDIDATE` | `ACCEPTED` | `TRIAL-AUTHORIZED` | `DEFERRED` | `REJECTED` | `SUPERSEDED`

## Implementation status
`NOT-LINKED` | `LINKED` | `IN-TRIAL` | `IMPLEMENTED` | `VERIFIED` | `ROLLED-BACK`

## Planning disposition
`current-plan-authorized` | `future-plan-candidate` | `research-only` | `do-not-promote`

These dimensions are independent.

---

# 5. Explicit adoption record

```yaml
pattern_id: APR-XXX
decision: accepted | trial-authorized | rejected | superseded
scope: <exact subsystem / horizon / objective>
authority: <AI Capital architecture decision>
decision_date: YYYY-MM-DD
implementation_link: <roadmap / decision / issue / change>
acceptance_effect: none | <explicit contract change>
rationale: <AI Capital rationale>
```

Promotion requires a real forcing function, constitutional compatibility, exact adaptation, exact scope, failure model, required Evidence, explicit authorization, and implementation linkage.

---

# 6. Constitutional baseline

Patterns must preserve where applicable:

```text
Program > Conversation
Actor > Model
Cognition ≠ Authority
Tool ≠ Capability
Availability ≠ Permission ≠ Exposure ≠ Invocation
Invocation ≠ Effect confirmation
Execution outcome ≠ Environmental effect
Evidence ≠ Claim ≠ Policy ≠ Authority
Conclusion ≠ Disposition
Agent completion proposal ≠ Program completion
Retention ≠ Context residency
Visibility ≠ Authorization
Delegation attenuates Authority
Access ≠ Ownership ≠ Migration ≠ Reproduction
Organization success ≠ Ecosystem success
Evidence generation ≠ Scientific acceptance
Containment ≠ Authorization
External integration vocabulary terminates at adapters
```

---

# 7. Pattern assessment rubric

Before promotion assess: problem fit; Authority fit; Evidence/audit fit; determinism fit; security/containment fit; reliability/recovery fit; operational fit; product fit; migration fit; cost/dependency fit.

Do not collapse these dimensions into one weighted score.

---

# 8. Baseline patterns

`ACCEPTED + LINKED` means intentionally present in a current roadmap; it does not mean implemented or verified.

## APR-001 — Canonical Durable Program State
**Category:** durability, work-state, Authority  
**Pattern status:** `ACCEPTED`  
**Implementation status:** `LINKED`  
**Planning disposition:** `current-plan-authorized`

**Problem.** Long-running work becomes unreliable when conversation, transient plans, or worker-local state are treated as the task itself.  
**Definition.** Maintain one Host-owned durable Program as canonical work state; conversations, plans, summaries, and UI views are projections/proposals.  
**Failure modes.** Conversation silently becomes canonical; local task graph diverges; revision changes without current-state checks.  
**Basis:** architecture-synthesis + programme-decision.  
**Trigger:** foundational.  
**Required Evidence:** crash/restart reconstruction; context deletion without Program loss; stale revision rejection.  
**Roadmap:** K1 / H1.  
**Decision:** accepted, 2026-08-30.

## APR-002 — Privileged Host Authority Boundary
**Category:** Authority, execution, durability  
**Status:** `ACCEPTED` / `LINKED` / `current-plan-authorized`

**Problem.** Cognition is unsafe if it directly authors the same canonical state intended to constrain it.  
**Definition.** Host owns canonical admission, Program transition, Capability authorization, protected execution, recovery, Evidence admission, Verification currentness, and completion.  
**Failure modes.** Actor writes canonical store; extension replaces policy/completion; Host trusts self-asserted currentness.  
**Basis:** architecture-synthesis + programme-decision.  
**Required Evidence:** direct bypass tests fail; unauthorized mutation is structurally blocked; stale Actor cannot execute.  
**Roadmap:** K0/K4/K7 / H1.  
**Decision:** accepted, 2026-08-30.

## APR-003 — Replaceable Actor over Model Binding
**Category:** cognition, identity, model-neutrality  
**Status:** `ACCEPTED` / `LINKED` / `current-plan-authorized`

**Problem.** If model invocation equals Actor, model replacement destroys identity and continuity.  
**Definition.** Persistent Actor identity remains independent from current model binding; model calls are disposable cognitive activations.  
**Failure modes.** Model session ID becomes Actor ID; model-specific state leaks into Program; model switch widens Authority.  
**Basis:** architecture-synthesis + programme-decision.  
**Required Evidence:** mid-Program model replacement; same Actor resumes; model metadata remains provenance only.  
**Roadmap:** K2 / H1-H2.  
**Decision:** accepted, 2026-08-30.

## APR-004 — Typed Semantic Capability Boundary
**Category:** Capability, execution, interoperability  
**Status:** `ACCEPTED` / `LINKED` / `current-plan-authorized`

**Problem.** Raw implementation-function exposure couples cognition to mechanics and makes policy ambiguous.  
**Definition.** Expose typed AI Capital Capability contracts declaring operation, resource type, effect, reversibility, risk, and versioned schemas.  
**Failure modes.** Command names become security ontology; handlers under one capability have different effects; metadata stale/incomplete.  
**Basis:** architecture-synthesis + programme-decision.  
**Required Evidence:** handler substitution; schema fail-loud; policy uses normalized effects.  
**Roadmap:** K3 / H1-H4.  
**Decision:** accepted, 2026-08-30.

## APR-005 — Capability Lifecycle State Separation
**Category:** Capability, Authority, security  
**Status:** `ACCEPTED` / `LINKED` / `current-plan-authorized`

**Definition.** Distinguish available, admitted, granted, exposed, requested, authorized, invoked, and effect-confirmed states.  
**Failure modes.** Registration implies exposure; revoked Grant remains invocable; invocation success implies effect confirmation.  
**Basis:** architecture-synthesis + programme-decision.  
**Required Evidence:** revocation blocks future invocation; ungranted available capability cannot execute; audit distinguishes lifecycle states.  
**Roadmap:** K3-K4 / H1-H4.  
**Decision:** accepted, 2026-08-30.

## APR-006 — Durable Operation Journal with Explicit Effect Uncertainty
**Category:** effects, durability, recovery  
**Status:** `ACCEPTED` / `LINKED` / `current-plan-authorized`

**Problem.** Timeout/crash/failure does not prove mutation did not occur.  
**Definition.** Give environmental work Host-owned Operation identity and model ExecutionOutcome, EffectStatus, and ReconciliationStatus independently.  
**Failure modes.** Timeout auto-retries mutation; handler result treated as effect truth; crash loses dispatch identity.  
**Basis:** architecture-synthesis + programme-decision.  
**Required Evidence:** timeout-before/after-effect; crash-after-dispatch; no blind replay.  
**Roadmap:** K5 / H1.  
**Decision:** accepted, 2026-08-30.

## APR-007 — Append-Oriented Semantic Event Ledger with Rebuildable Projections
**Category:** durability, provenance, projection  
**Status:** `ACCEPTED` / `LINKED` / `current-plan-authorized`

**Definition.** Record ordered semantic facts in an append-oriented ledger and derive mutable projections; stronger domain objects retain current-state Authority.  
**Failure modes.** Projection becomes second truth; partial streaming text becomes settled fact; Event history misused as effect truth.  
**Required Evidence:** projection rebuild equality; duplicate handling; corrupt projection recovery.  
**Roadmap:** K1 / H1-H2.  
**Decision:** accepted, 2026-08-30.

## APR-008 — Evidence–Claim Separation with Provenance
**Category:** Evidence, epistemics, audit  
**Status:** `ACCEPTED` / `LINKED` / `current-plan-authorized`

**Definition.** Evidence and Claims are separate objects; Claims reference Evidence while Evidence retains source identity, digest, time, trust/currentness, and content address.  
**Failure modes.** Summary is only source; memory note marked verified; cited source cannot be resolved.  
**Required Evidence:** Claim→Evidence→source trace; digest mismatch detection; unsupported Claim remains distinct.  
**Roadmap:** K6 / H1-H7.  
**Decision:** accepted, 2026-08-30.

## APR-009 — Epistemic and Governance State Separation
**Category:** epistemics, Authority, policy  
**Status:** `ACCEPTED` / `LINKED` / `current-plan-authorized`

**Definition.** Observation, Evidence, Claim/belief, policy, Authority, and Disposition are explicit classes with named transitions.  
**Failure modes.** Belief becomes policy; recalled history becomes current truth; high-confidence Claim authorizes action.  
**Required Evidence:** type checks prevent implicit promotion; audit shows transition lineage.  
**Roadmap:** K6-K7 / H1-H7.  
**Decision:** accepted, 2026-08-30.

## APR-010 — Independent Completion Certification
**Category:** completion, Verification, Authority  
**Status:** `ACCEPTED` / `LINKED` / `current-plan-authorized`

**Definition.** Terminal success is Host certification over current Program state, Verification, blockers, protected Operations, and success criteria.  
**Failure modes.** Idle equals success; stale Verification certifies new Program; indeterminate effect ignored.  
**Required Evidence:** false-completion test; stale-Verification rejection; completion reconstructable without transcript.  
**Roadmap:** K7 / H1.  
**Decision:** accepted, 2026-08-30.

## APR-011 — Reversible Context Residency over Durable Exact History
**Category:** context, durability, retrieval  
**Status:** `ACCEPTED` / `LINKED` / `current-plan-authorized`

**Definition.** Separate exact durable retention from live context residency; persist before eviction; retain stable recovery addresses; allow bounded recall.  
**Failure modes.** Evict before persist; summary becomes only history; recalled Evidence regains current Authority.  
**Required Evidence:** restart-safe exact recall; persistence failure blocks eviction; ContextReceipt records included/excluded sources.  
**Roadmap:** K8 / H1-H3.  
**Decision:** accepted, 2026-08-30.

## APR-012 — Memory Retrieval Does Not Imply Memory Use
**Category:** memory, learning, provenance  
**Status:** `CANDIDATE` / `NOT-LINKED` / `future-plan-candidate`

**Definition.** Track memory retrieval/visibility separately from explicit semantic use; reinforce only under a defined later-use contract.  
**Failure modes.** Every hit counts as use; rankings self-reinforce irrelevant records.  
**Trigger:** memory ranking/learning starts updating records.  
**Required Evidence:** retrieved-unused record is not reinforced; explicitly referenced record is; false-memory challenge.  
**Roadmap:** H3.

## APR-013 — Disposition Gate Separate from Semantic Inference
**Category:** Disposition, policy, epistemics  
**Status:** `ACCEPTED` / `LINKED` / `current-plan-authorized`

**Definition.** Separate semantic inference from Disposition; consequential decisions bind Evidence, assumptions, policy/current context, and explicit action posture before Authority/execution.  
**Failure modes.** Conclusion directly triggers protected action; Disposition hidden in prompt; confidence conflated with policy.  
**Required Evidence:** same conclusion can yield different Disposition under changed state; Disposition cannot bypass Grant checks.  
**Roadmap:** K6-K7 / H1-H7.  
**Decision:** accepted, 2026-08-30.

## APR-014 — Minimal Capability Profile for Cognitive Workers
**Category:** security, Capability, cognition  
**Status:** `ACCEPTED` / `LINKED` / `current-plan-authorized`

**Definition.** Expose only Capabilities required for current Program/Actor scope; snapshots are explicit, current, bounded, and receipted.  
**Failure modes.** Global registry exposed to every Actor; missing required Capability degrades silently; set changes mid-inference without receipt.  
**Required Evidence:** capability snapshot receipt; missing required Capability fails loud.  
**Roadmap:** K3-K8 / H1.  
**Decision:** accepted, 2026-08-30.

## APR-015 — Model-Neutral Inference Boundary with Attempt Provenance
**Category:** cognition, provenance  
**Status:** `ACCEPTED` / `LINKED` / `current-plan-authorized`

**Definition.** Treat each model call as a bounded inference attempt behind an AI Capital interface; record effective model/config provenance; keep model-native identities separate from domain identities.  
**Failure modes.** External call ID reused as Operation ID; adapter retries protected work invisibly; route identity treated as safety Authority.  
**Required Evidence:** retry tests; model-switch continuity; provenance receipt completeness.  
**Roadmap:** K2 / H1-H4.  
**Decision:** accepted, 2026-08-30.

## APR-016 — Fresh Non-Reusable Execution Authority
**Category:** Authority, freshness, Capability  
**Status:** `ACCEPTED` / `LINKED` / `current-plan-authorized`

**Definition.** Bind protected execution to a fresh single-use Host receipt naming current Program revision, Actor generation, Capability binding, policy, Grants, and resolved effect.  
**Failure modes.** Old request executes after replacement; recreated Capability accepts stale call; long-lived token survives relevant revision.  
**Required Evidence:** replacement/recreation stale-call tests; Program revision invalidates protected old request.  
**Roadmap:** K3-K5 / H1-H5.  
**Decision:** accepted, 2026-08-30.

## APR-017 — One-Shot Audited Approval Grant
**Category:** approval, Authority, audit  
**Status:** `CANDIDATE` / `NOT-LINKED` / `future-plan-candidate`

**Definition.** Bind human approval to one exact proposed protected effect and current context; consume once; stale, mismatched, or reused approval fails closed.  
**Trigger:** first protected Capability requiring interactive approval.  
**Required Evidence:** replay rejection; changed-target rejection; exact approval audit.  
**Roadmap:** H2 / optional K4.

## APR-018 — Snapshot-Only Observability without Authority Handles
**Category:** observation, security, provenance  
**Status:** `CANDIDATE` / `NOT-LINKED` / `future-plan-candidate`

**Definition.** Expose immutable/read-only snapshots and receipts to observers; observation surfaces cannot mutate canonical state except through ordinary admitted Capability/Authority paths.  
**Trigger:** first asynchronous observability/evaluation subsystem.  
**Required Evidence:** observer has no mutation path; snapshot freshness validated.  
**Roadmap:** H2-H4.

## APR-019 — Mutable Current Projection over Immutable Receipts
**Category:** audit, product, projection  
**Status:** `ACCEPTED` / `LINKED` / `current-plan-authorized`

**Definition.** Maintain mutable current projections backed by immutable/append-oriented receipts; projections are rebuildable and never the sole audit source.  
**Failure modes.** Editable status is only record; incompleteness hidden; wrong Program revision displayed.  
**Required Evidence:** projection rebuild; presentation loss does not lose audit.  
**Roadmap:** K1-K8 / H1-H2.  
**Decision:** accepted, 2026-08-30.

## APR-020 — State-Indexed Verification Freshness
**Category:** Verification, freshness, completion  
**Status:** `ACCEPTED` / `LINKED` / `current-plan-authorized`

**Definition.** Bind Verification to exact subject revision/digest and invalidate or re-evaluate when semantically relevant dependencies change.  
**Failure modes.** Old result certifies new state; policy/Evidence change ignored; receipt lacks subject identity.  
**Required Evidence:** relevant mutation invalidates old Verification; same-state reuse only when contract permits.  
**Roadmap:** K7 / H1.  
**Decision:** accepted, 2026-08-30.

## APR-021 — Durable Child Work with Ephemeral Actor Activation
**Category:** delegation, multi-Actor, durability  
**Status:** `DEFERRED` / `NOT-LINKED` / `future-plan-candidate`

**Definition.** Give child work Host-owned durable identity while Actor activations remain replaceable; each activation receives fresh execution Authority.  
**Trigger:** measured workload requires concurrent/replaceable child Actors.  
**Required Evidence:** kill/replace child without duplicate effects; superseded child cannot publish authoritative result.  
**Roadmap:** H5.

## APR-022 — Authority-Attenuating Delegation Envelope
**Category:** delegation, Authority, security  
**Status:** `DEFERRED` / `NOT-LINKED` / `future-plan-candidate`

**Definition.** Delegation explicitly binds objective, Capability/resource/effect ceilings, budget, Evidence requirements, expiry, and parent Authority; child Authority is a subset of parent Authority.  
**Required Evidence:** property tests for attenuation; parent revocation blocks future child admission.  
**Roadmap:** H5-H6.

## APR-023 — Organization as Durable State beyond Actors
**Category:** Organization, durability, coordination  
**Status:** `DEFERRED` / `NOT-LINKED` / `future-plan-candidate`

**Definition.** Organization, Mission, Membership/RoleAssignment, institutional Events, policy sets, and organizational memory are durable state independent from current Actor activations.  
**Failure modes.** Organization becomes group conversation; role name grants Authority; one global Actor memory becomes organizational memory.  
**Required Evidence:** replace members/models while preserving mission/Authority/history.  
**Roadmap:** H6.

## APR-024 — Capability Evidence over Self-Description
**Category:** Capability, Evidence, routing  
**Status:** `DEFERRED` / `NOT-LINKED` / `future-plan-candidate`

**Definition.** Capability Claims include demonstrations, environment, scope, uncertainty/performance, dependencies, provenance, and validity.  
**Trigger:** routing/team formation benefits materially from measured capability differences.  
**Required Evidence:** holdout demonstrations; scope/generalization failures remain visible.  
**Roadmap:** H8.

## APR-025 — Evidence-Bounded Capability Composition
**Category:** Capability, composition, evaluation  
**Status:** `DEFERRED` / `NOT-LINKED` / `future-plan-candidate`

**Definition.** Composition is an explicit contract over interfaces, prerequisites, incompatibilities, Evidence scope, and composition-specific evaluation. Component competence does not prove composed competence.  
**Required Evidence:** unseen composition holdouts; failure attribution.  
**Roadmap:** H8.

## APR-026 — Capability Reproduction Contract
**Category:** Capability, learning, reproduction  
**Status:** `DEFERRED` / `NOT-LINKED` / `research-only`

**Definition.** Describe conditions, resources, developmental procedure, excluded private state, evaluation, and provenance required to produce a fresh instance of a CapabilityClass.  
**Failure modes.** Private state leaks; moving existing expert is called reproduction; evaluation uses only development examples.  
**Required Evidence:** prospective reproduction protocol; independent evaluation; private-state leakage audit.  
**Roadmap:** H9 research.

## APR-027 — Capability Production Technology as First-Class Mechanism
**Category:** Capability, development, economics  
**Status:** `DEFERRED` / `NOT-LINKED` / `research-only`

**Definition.** Represent validated methods for producing capability as `ProductionTechnology` records with inputs, resources, output class, cost/latency/variance, source impact, Evidence, and scope.  
**Failure modes.** Raw stock growth ignores resource cost; reallocation reported as production; unvalidated recipe treated as production technology.  
**Required Evidence:** production outcome/cost measured; source/destination accounting.  
**Roadmap:** H9-H10 research.

## APR-028 — Federation Preserves Local Sovereignty
**Category:** federation, Authority, interoperability  
**Status:** `DEFERRED` / `NOT-LINKED` / `future-plan-candidate`

**Definition.** Federate through explicit contracts/manifests/Evidence while keeping each local environment useful when disconnected; cross-boundary rights and Authority remain explicit.  
**Required Evidence:** disconnect test; cross-version contract test; no direct import of local internal Authority.  
**Roadmap:** H10.

## APR-029 — Independent Scientific Mechanism Promotion
**Category:** science, governance, adaptation  
**Status:** `DEFERRED` / `NOT-LINKED` / `research-only`

**Definition.** Separate mechanism proposal/Evidence generation from scientific acceptance and runtime promotion; promotion is explicit, auditable, and independent where applicable.  
**Failure modes.** Experiment self-promotes; posthoc subgroup becomes production feature; failed mechanism silently retuned until positive.  
**Required Evidence:** independent promotion receipt; negative result retained; runtime feature links to qualifying Evidence.  
**Roadmap:** H11.

## APR-030 — Reversible Extension Composition below the Trusted Kernel
**Category:** extensibility, lifecycle, security  
**Status:** `CANDIDATE` / `NOT-LINKED` / `future-plan-candidate`

**Definition.** Providers/adapters/extensions may register owned reversible contributions beneath Host seams; trusted canonical Authority is non-delegable to ordinary extensions.  
**Trigger:** multiple independently versioned extension classes create a concrete lifecycle problem.  
**Required Evidence:** load/unload leaves no stale registrations/Authority; extension cannot bypass Host APIs.  
**Roadmap:** H4.

## APR-031 — Containment Does Not Replace Authorization
**Category:** security, containment, Authority  
**Status:** `ACCEPTED` / `LINKED` / `current-plan-authorized`

**Definition.** Containment is defense in depth around execution, not the Authority mechanism. A contained Actor still requires Capability admission, Grants, policy, and Operation truth.  
**Required Evidence:** same Authority tests pass across containment backends; containment cannot mutate Program outside Host APIs.  
**Roadmap:** K4-K5 / H1-H4.  
**Decision:** accepted, 2026-08-30.

## APR-032 — External Integration Vocabulary Terminates at Adapters
**Category:** interoperability, architecture, Capability  
**Status:** `ACCEPTED` / `LINKED` / `current-plan-authorized`

**Definition.** Translate external model/protocol/execution/integration concepts into AI Capital-owned Actor/Capability/Operation/Evidence/Authority contracts at adapter boundaries. Preserve external identifiers only as provenance when needed.  
**Failure modes.** External schema becomes Capability ontology; external IDs become canonical Authority IDs; reconnect semantics silently define freshness.  
**Required Evidence:** swap adapter while core tests remain unchanged; external IDs absent from canonical Authority keys except provenance.  
**Roadmap:** K2-K3 / H1-H4.  
**Decision:** accepted, 2026-08-30.

---

# 9. Anti-pattern register

## APX-001 — Conversation as canonical task truth
Treating conversation history, model session state, or mutable task list as authoritative Program. **Control:** APR-001.

## APX-002 — Model self-authorization
Model output/confidence/free-form instructions determine protected permission. **Control:** APR-002, APR-005.

## APX-003 — Capability availability equals permission
Available capability automatically becomes visible/invocable. **Control:** APR-005, APR-014.

## APX-004 — Failure or timeout means no effect
Retry mutation because execution did not return success. **Control:** APR-006.

## APX-005 — Actor says done, therefore complete
Self-report, idle state, or plan exhaustion equals terminal success. **Control:** APR-010.

## APX-006 — Memory equals Evidence
Retrieved memory, notes, summaries, or model statements treated as verified Evidence. **Control:** APR-008, APR-009.

## APX-007 — Summary replaces exact history
Deleting exact history because a compact summary exists. **Control:** APR-011.

## APX-008 — Recalled history regains current Authority
Historical Evidence becomes current merely because recalled exactly. **Control:** APR-009, APR-011.

## APX-009 — External identifiers become domain Authority
External call/session/connection IDs reused as Program, Actor, Operation, Event, or Grant IDs. **Control:** APR-015, APR-032.

## APX-010 — Hidden adapter retries
Adapters retry protected/stateful work without Host-owned Operation/effect semantics. **Control:** APR-006, APR-015.

## APX-011 — Extension replaces trusted kernel
Ordinary extension can replace Program, policy, Completion, Evidence admission, or Operation Authority. **Control:** APR-002, APR-030.

## APX-012 — Containment as authorization
Restricted execution environment is treated as sufficient permission. **Control:** APR-031.

## APX-013 — Multi-Actor before single-Actor correctness
Delegation/parallel Actors added before restart, stale Authority, effect uncertainty, Evidence, and completion semantics are correct. **Control:** defer APR-021/022 until H5.

## APX-014 — Marketplace before CapabilityEvidence
Market/reputation mechanisms built before demonstrated capability scope exists. **Control:** APR-024.

## APX-015 — Reallocation reported as capability creation
Moving an existing capability counted as production. **Control:** APR-026/027.

## APX-016 — Organization success erases source cost
Destination/team success reported without source opportunity cost. **Control:** later ecological accounting.

## APX-017 — Self-evaluated self-promotion
Learned mechanism evaluates itself and installs itself into authoritative runtime/policy. **Control:** APR-029.

## APX-018 — Distributed infrastructure by anticipation
Distributed services/workers introduced because they may be useful someday. **Control:** master-roadmap scaling triggers.

## APX-019 — Unbounded recall recreates context overflow
Recall injects arbitrary archived history into inference. **Control:** APR-011.

## APX-020 — Self-modifying policy on authoritative path
Cognition rewrites policy/Capability rules governing its own current execution. **Control:** APR-002, APR-013, APR-029.

---

# 10. Cross-pattern syntheses

## S-01 — Minimal Autonomous Work Control Loop
Combines APR-001, 002, 004, 005, 006, 008, 010, 015, 016, 020.

```text
Program
   ↓
Actor / model inference
   ↓
Capability request
   ↓
resolve target / effect
   ↓
currentness + AuthorityDecision
   ↓
Operation Journal
   ↓
execute / reconcile
   ↓
Evidence + Verification
   ↓
Host completion certification
```

**Disposition:** current H1 kernel architecture.

## S-02 — Durable History with Bounded Intelligence Surface
Combines APR-007, 008, 011, 012, 019.

```text
append-oriented facts + exact artifacts
              ↓
       durable source history
              ↓
       index / navigation / cache
              ↓
        context compiler
              ↓
 bounded model-visible context
              ↓
        bounded explicit recall
```

**Rule:** retention is independent of residency; recall restores Evidence access, not current Authority.

## S-03 — Epistemic Decision Chain
Combines APR-008, 009, 013, 017.

```text
Evidence
   ↓
Claim / semantic conclusion
   ↓
Disposition
   ↓
Authority / approval
   ↓
Execution admission
```

## S-04 — Safe Delegated Team Work
Combines APR-016, 021, 022, 023.

```text
Organization / parent Program
      ↓
Host-owned child work
      ↓
authority-attenuating DelegationEnvelope
      ↓
ephemeral child Actor activation
      ↓
fresh execution Authority
      ↓
Evidence / result
      ↓
parent integration + independent completion
```

**Disposition:** future H5/H6 only.

## S-05 — Capability Development Ecology
Combines APR-024, 025, 026, 027, 028, 029.

```text
CapabilityEvidence
       ↓
Discovery / composition
       ↓
Development experiment
       ↓
Independent acceptance
       ↓
Reproduction / ProductionTechnology
       ↓
Federated access with source-cost accounting
```

**Disposition:** long-horizon research; no current kernel scope.

---

# 11. Pattern intake template

```markdown
## APR-XXX — Vendor-Neutral Pattern Name

**Category:** ...
**Pattern status:** OBSERVED
**Implementation status:** NOT-LINKED
**Planning disposition:** research-only

### Pattern definition
**Problem.** ...
**Definition.** ...
**Benefits.** ...
**Failure modes.** ...

### Architectural basis
**Basis:** architecture-synthesis | programme-decision | implementation-learning | incident-learning | research-hypothesis | empirical-evaluation
**Internal evidence refs:** ...

### AI Capital interpretation
**Adoption trigger.** ...
**Required Evidence.** ...
**Conflicts / boundaries.** ...
**Alternatives.** ...
**Related patterns.** ...

### Decision log
- Pattern status:
- Implementation status:
- Planning disposition:
- Decision: none | accepted | trial-authorized | rejected | superseded
- Authority:
- Decision date:
- Implementation link:
- Acceptance effect:
- Rationale:
```

---

# 12. Pattern review cadence

Review before freezing a new horizon; after substantial architecture synthesis; after an incident/evaluation exposes reusable control; before new Authority classes; before dynamic extensions, remote execution, multi-Actor work, Organizations, or federation; before capability-development/reproduction promotion; when an accepted pattern is disproved or superseded.

A review may retain, characterize, promote, demote, defer, reject, supersede, link/unlink, or update internal Evidence. It must not silently change a frozen implementation objective.

---

# 13. Maintenance and history rules

1. Stable IDs; never renumber/reuse retired APR/APX IDs.
2. Repository-facing pattern text contains no external product/project identities.
3. Record internal basis and Evidence references, not external branded source ledgers.
4. Research/implementation success is Evidence, not architecture Authority.
5. `ACCEPTED` never implies `IMPLEMENTED` or `VERIFIED`.
6. Rejected/superseded patterns remain architecture memory.
7. No weighted architecture score.
8. Candidate/deferred patterns create no shadow backlog.
9. Promotion requires executable proof.
10. Complexity is earned by demonstrated forcing function.

---

# 14. Internal basis classes

```text
architecture-synthesis
programme-decision
implementation-learning
incident-learning
research-hypothesis
empirical-evaluation
supersession-review
```

Entries may reference internal decision IDs, tests, incidents, experiments, commits, or issues once those exist.

---

# 15. Initial roadmap mapping

| Pattern | Kernel / future horizon |
|---|---|
| APR-001 | K1 / H1 |
| APR-002 | K0, K4, K7 / H1 |
| APR-003 | K2 / H1-H2 |
| APR-004 | K3 / H1-H4 |
| APR-005 | K3-K4 / H1-H4 |
| APR-006 | K5 / H1 |
| APR-007 | K1 / H1-H2 |
| APR-008 | K6 / H1-H7 |
| APR-009 | K6-K7 / H1-H7 |
| APR-010 | K7 / H1 |
| APR-011 | K8 / H1-H3 |
| APR-012 | H3 |
| APR-013 | K6-K7 / H1-H7 |
| APR-014 | K3-K8 / H1 |
| APR-015 | K2 / H1-H4 |
| APR-016 | K3-K5 / H1-H5 |
| APR-017 | H2 |
| APR-018 | H2-H4 |
| APR-019 | K1-K8 / H1-H2 |
| APR-020 | K7 / H1 |
| APR-021 | H5 |
| APR-022 | H5-H6 |
| APR-023 | H6 |
| APR-024 | H8 |
| APR-025 | H8 |
| APR-026 | H9 research |
| APR-027 | H9-H10 research |
| APR-028 | H10 |
| APR-029 | H11 |
| APR-030 | H4 |
| APR-031 | H1-H4 |
| APR-032 | H1-H4 |

---

# 16. Canonical maintenance principle

> **Study broadly in private research memory. Internalize architecture. Preserve AI Capital's own vocabulary. Adopt only under constitutional invariants. Implement narrowly. Verify independently. Scale only when a forcing function earns the complexity.**
