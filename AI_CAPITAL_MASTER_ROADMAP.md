# AI Capital — Master Roadmap

**Document class:** Programme roadmap  
**Programme:** AI Capital  
**Document version:** 0.1  
**Status:** Architecture-freeze candidate  
**Prepared:** 2026-08-30  
**Constitutional authority:** `ARCHITECTURE_CONSTITUTION.md`  
**Planning horizon:** kernel → intelligent autonomous work → teams → organizations → capability ecology → regenerative federation

**Core rule:** Every horizon adds a new class of power only after the required authority, durability, Evidence, failure, and recovery semantics are executable and stable.

---

# 1. Programme thesis

AI Capital is not intended to become one giant “super-agent.” It is intended to evolve in controlled stages into a lightweight substrate for AI-native institutions.

Long-term architecture:

```text
Persistent Actors
      +
Durable Programs
      +
Typed Capabilities
      +
Explicit Authority
      +
Evidence / Provenance
      +
Disposition
      +
Organizational State
      +
Capability Development / Reproduction
      +
Scientific Governance
```

AI Capital deliberately starts much smaller:

```text
one Host
one local durable store
one workspace
one active Actor
one Program authority
small typed capability set
```

Scale is earned by demonstrated forcing functions, not assumed in advance.

---

# 2. Programme-level invariants

The Constitution is authoritative. This summary remains useful across all horizons:

```text
Model ≠ Actor
Conversation ≠ Program
Actor ≠ Organization
Tool ≠ Capability
Capability availability ≠ Permission
Evidence ≠ Claim
Claim ≠ Belief
Belief ≠ Policy
Policy ≠ Authority
Conclusion ≠ Disposition
Disposition ≠ Authority
Execution outcome ≠ Environmental effect
Verification passed ≠ Verification still current
Agent completion proposal ≠ Program completion
Retention ≠ Context residency
Access ≠ Ownership ≠ Migration ≠ Reproduction
Organization success ≠ Ecosystem success
Capability transfer ≠ Capability reproduction
Evidence generation ≠ Scientific acceptance
Containment ≠ Authorization
```

> **Long-lived intent is durable; short-lived cognition is replaceable.**

---

# 3. Roadmap structure

A horizon is not a calendar promise. It closes only when explicit proof gates pass.

```text
H0  Architecture Constitution
H1  Small Autonomous-Work Kernel
H2  Reliable Single-Actor Product
H3  Context, Memory, and Epistemic Intelligence
H4  Extensibility and Governed Execution Fabric
H5  Multi-Actor Delegation and Teamwork
H6  Organization and Institutional State
H7  Institutional Epistemics and Disposition
H8  Capability Evidence, Discovery, and Composition
H9  Capability Development, Reproduction, and Regeneration
H10 Federation of Independent Environments
H11 Scientific Governance and Adaptive Institutions
H12 Mature AI-Native Institutional Platform
```

Later-horizon research may begin early, but production authority does not advance until lower-horizon controls are stable.

---

# H0 — Architecture Constitution and Research Discipline

## Objective
Freeze AI Capital's architectural constitution and turn research synthesis into independent AI Capital contracts before implementation scope expands.

## Deliverables
- `ARCHITECTURE_CONSTITUTION.md`;
- normalized object vocabulary;
- `AI_CAPITAL_ARCHITECTURE_PATTERN_REGISTER.md`;
- architecture-decision/adoption process;
- anti-pattern register;
- internal provenance classes;
- acceptance/Evidence discipline;
- authoritative ownership matrix;
- semantic error taxonomy;
- “no complexity without a forcing function” rule;
- repository vocabulary-hygiene rule.

## Core objects established
```text
Actor
Program
Capability
Grant
AuthorityDecision
Operation
Evidence
Claim
Disposition
Verification
Event
ContextReceipt
ExecutionAuthorityReceipt
CompletionReceipt
```

## Exit gate
Every authoritative state has one canonical owner; all repository-facing architecture stands on AI Capital-native or vendor-neutral terminology; K0 proof contract is executable.

---

# H1 — Small Autonomous-Work Kernel

## Objective
Build the trusted minimal runtime described in `AI_CAPITAL_AUTONOMOUS_WORK_KERNEL_ROADMAP.md`.

## Capabilities introduced
- durable Program state;
- replaceable Actor/model binding;
- typed capability registry/broker;
- deterministic Authority decisions;
- fresh execution authority;
- Operation/effect journal;
- effect reconciliation;
- Evidence/provenance;
- Verification;
- independent completion certification;
- bounded context projection;
- append-oriented semantic history.

## Deployment shape
```text
single Host
single local durable store
single workspace
single active Actor per Program
```

## Exit gate
Crash continuity, model replacement, Authority resistance, stale-authority fencing, ambiguous-effect recovery, Evidence traceability, false-completion resistance, context-pressure recovery, and vocabulary hygiene all pass executable tests.

---

# H2 — Reliable Single-Actor Product

## Objective
Turn the kernel into a useful autonomous worker without changing its Authority model.

## Product outcomes
- developer-facing command interface and local API;
- structured Program creation/inspection;
- human approval surface;
- pause/resume/cancel;
- clear waiting/running/blocked/reconciling/completed states;
- artifact browser;
- Operation/Evidence audit views;
- model-binding configuration;
- workspace snapshots;
- reusable skills/instructions;
- basic schedules only if a concrete user scenario requires them.

## Built-in capability families
```text
workspace
local command execution
network retrieval
versioned change inspection
structured data
artifact generation
```

High-impact external capabilities remain optional and explicitly granted.

## Reliability work
- restart during every lifecycle edge;
- safe cancellation;
- duplicate request detection;
- resource limits;
- Operation timeouts;
- explicit user-visible blocked/ask/reconcile state;
- deterministic export/import of a Program bundle;
- one-shot human approvals for consequential effects when needed.

## Exit gate
A user can entrust a bounded multi-step objective to one Actor, leave and return later, inspect exactly what happened, and continue safely after failures or model replacement.

---

# H3 — Context, Memory, and Epistemic Intelligence

## Objective
Improve long-horizon reasoning without allowing memory machinery to become Authority or truth.

## 3.1 Context virtualization
```text
Durable exact history
        ↓
index / navigation
        ↓
context compiler
        ↓
bounded inference context
```

Requirements: persist before eviction; stable recall addresses; bounded recall; ContextReceipts; explicit source coverage/completeness; historical Evidence remains historical after recall.

## 3.2 Memory classes
```text
Working memory
Episodic memory
Procedural memory
```

Rules: retrieved ≠ used; remembered ≠ verified; procedural memory cannot mint Authority; autonomous consolidation begins only as a bounded experiment.

## 3.3 Epistemic graph lite
```text
Evidence → Claim
Claim ↔ Contradiction
Claim → Supersession
Claim → Decision
```

Avoid a universal knowledge graph at this stage.

## Intelligence evaluations
- long-horizon recovery;
- source-reference accuracy;
- contradiction handling;
- memory contamination resistance;
- context-budget efficiency;
- model-switch continuity.

## Exit gate
Long-running Programs materially exceeding model context remain recoverable and Evidence-aware without treating summaries, memories, or recalled history as current Authority.

---

# H4 — Extensibility and Governed Execution Fabric

## Objective
Allow AI Capital to acquire new Capabilities without turning extension code into an alternate trusted kernel.

## New concepts
```text
Capability Provider
Adapter
Extension Package
Execution Backend
Runtime Profile
```

## Required separations
```text
Extension packaging ≠ execution authority
Capability registration ≠ admission
Admission ≠ Grant
Grant ≠ model exposure
Exposure ≠ invocation
Invocation ≠ effect confirmation
```

## Workstreams
- reversible extension registration;
- provider lifecycle;
- capability schema/versioning;
- trust/signature metadata;
- install/enable/admit/grant/expose states;
- local execution containment backend;
- remote execution abstraction only when required;
- external integration protocols terminate at adapters;
- capability negotiation and fail-loud dispatch.

## Security principle
Containment strengthens defense but does not replace Authority checks.

## Exit gate
Multiple independently versioned Capability providers can be installed, upgraded, removed, and replaced without changing Program, Authority, Evidence, or Operation semantics and without leaving stale execution authority.

---

# H5 — Multi-Actor Delegation and Teamwork

## Objective
Add more than one cognitive Actor without losing single-Actor guarantees.

## New objects
```text
Delegation
ChildWork
Role
Team
ActorGeneration
WorkLease
```

## Core pattern
```text
Parent Program
    ↓
Host-owned child work identity
    ↓
Delegation envelope
    ↓
Child Actor activation
    ↓
Fresh execution authority
    ↓
Evidence / result
    ↓
Parent integration
```

## Delegation envelope
```text
DelegationEnvelope {
  delegation_id
  issuer
  delegate
  objective_scope
  capability_ceiling
  resource_scope
  effect_ceiling
  budget
  evidence_requirements
  expiry
  parent_authority_ref
}
```

> **Delegation may attenuate Authority; it may never mint Authority the issuer does not possess.**

## Team features
bounded parallel work; child cancellation/replacement; explicit work ownership; Evidence-return contract; conflict detection; parent synthesis; no shared ambient super-context.

## Exit gate
Parallel Actors can be killed, replaced, superseded, or contradicted without duplicate Authority, stale publication, hidden effects, or ambiguous completion.

---

# H6 — Organization and Institutional State

## Objective
Make an Organization a durable object distinct from its current Actors.

## New objects
```text
Organization
Membership
RoleAssignment
InstitutionalEvent
PolicySet
Mission
OrganizationalMemory
```

## Architecture
```text
Organization
  ├─ missions
  ├─ roles
  ├─ Actors / humans
  ├─ authority Grants
  ├─ Programs
  ├─ Capabilities
  ├─ institutional Evidence / history
  └─ policies
```

## Key requirements
Durable organizational receipts where necessary; common primitives for human/Actor action where practical; role changes do not rewrite causality; institutional memory survives turnover; mission state remains separate from conversation/session state; organizational decision lineage is reconstructable.

## Research question
Can organizational performance persist after replacing current Actors/models that generated prior successes?

## Exit gate
A team can change members/models while preserving mission, Authority, institutional history, and explainable decision continuity.

---

# H7 — Institutional Epistemics and Disposition

## Objective
Make “what is justified?” and “what may we do?” explicit institutional functions.

## Epistemic kernel
```text
Evidence
  ↓
Claim
  ↓
Supported proposition
  ↓
Derived institutional conclusion
```

Support source provenance, confidence/uncertainty, contradictions, alternative models, supersession, temporal validity, provenance roots, and Evidence completeness classes.

## Disposition kernel
```text
Semantic evaluation:    What follows?
Disposition evaluation: What may we do?
```

Disposition classes may expand to:
```text
proceed
proceed_with_warning
request_evidence
escalate_and_hold
block
exclude_from_release
```

## Decision receipt
Bind consequential Disposition to state snapshot, semantic result, Claim/Evidence roots, assumptions, alternative models, Verification/replay results, policy revision, and ContextReceipt.

## Exit gate
The Organization can show why a consequential action was justified, what Evidence was missing, which policy applied, which Disposition was selected, and which Authority permitted execution—without collapsing these into one model judgment.

---

# H8 — Capability Evidence, Discovery, and Composition

## Objective
Shift from “which Actor?” to “which demonstrated capability, under what scope and Evidence?”

## New objects
```text
CapabilityClass
CapabilityInstance
CapabilityClaim
CapabilityEvidence
CapabilityContract
CompositionContract
```

## CapabilityEvidence
```text
CapabilityEvidence {
  capability
  holder
  demonstrations[]
  environment
  success_rate
  uncertainty
  validated_scope
  nonvalidated_scope
  dependencies[]
  source_dependency
  provenance_root
  valid_until?
}
```

Use CapabilityEvidence for Actor selection, team formation, task delegation, provider selection, and capability composition. Do not reduce capability to one global reputation score.

## Composition work
Typed interfaces; prerequisites; incompatibility constraints; composition Evidence; unseen-composition evaluation; explicit failure attribution.

## Exit gate
The system selects and composes capabilities based on demonstrated scope/Evidence rather than labels, self-description, or global reputation alone.

---

# H9 — Capability Development, Reproduction, and Regeneration

## Objective
Move beyond reallocating scarce capability toward producing new capability.

```text
Transfer = move/use an existing capability instance
Reproduction = produce a fresh capability instance from a validated developmental contract
Regeneration = sustainably increase capability supply after use/transfer/consumption
```

## New objects
```text
ReproductionContract
ProductionTechnology
DevelopmentEpisode
CapabilityArtifact
RegenerationContract
```

## ProductionTechnology
```text
ProductionTechnology {
  technology_id
  version
  input_capabilities[]
  required_resources[]
  output_capability_class
  expected_cost
  expected_latency
  expected_variance
  source_impact
  ownership_rule
  evidence_basis[]
  validated_scope
}
```

## Development mechanisms to investigate
apprenticeship; structured practice/feedback; returned learning; organization-funded development; procedural artifact transfer; modular recombination; team-formation learning; institutional accumulation; capability reproduction in fresh Actors/environments.

## Scientific rule
A developmental mechanism cannot become default runtime behavior merely because its proposing experiment produced a positive result.

## Exit gate
At least one capability class has a prospectively specified, independently evaluated reproduction mechanism creating a fresh instance without prohibited source-private state.

---

# H10 — Federation of Independent Environments

## Objective
Connect independent capability-producing environments without destroying sovereignty.

> **Federation must not destroy local sovereignty.**

Each local environment remains useful when disconnected.

## New objects
```text
EnvironmentManifest
FederationIdentity
PortableCapabilityContract
AccessRight
SecondmentRight
MigrationRight
ReproductionRight
CompositionRight
FederationEvent
```

## Access distinctions
```text
use in place
bounded service contract
secondment
migration
reproduction
composition
spawn
```

## Ecological accounting
Track destination gain, source opportunity cost, source capability loss, recovery/replacement, raw capability stock, resource-normalized productivity, and sustainability.

## Exit gate
Cross-environment access can improve destination outcomes while transparently accounting for source cost, Authority, Evidence, and ownership—and no relocation is mislabeled as capability creation.

---

# H11 — Scientific Governance and Adaptive Institutions

## Objective
Allow system improvement while preventing self-confirming architecture and uncontrolled self-modification.

## New objects
```text
Mechanism
MechanismClaim
ExperimentContract
EvidencePackage
PromotionEvent
RegistryStatus
```

## Mechanism lifecycle
A future lifecycle may include states equivalent to:
```text
posthoc_motivated
→ proposed
→ discovery_supported
→ internally_replicated
→ schema_generalized
→ model_generalized
→ naturalistic_validated
→ integration_eligible
→ evolution_eligible
```

Exact names may evolve; semantic separation must remain.

## Governance invariants
```text
Evidence generation ≠ Scientific acceptance
Proposer ≠ Acceptor
Architecture proposal ≠ Architecture promotion
Self-improvement ≠ Self-granted Authority
```

## Workstreams
Frozen ExperimentContracts; negative-result preservation; statistical discipline where applicable; independent mechanism acceptance; reproducibility manifests; PromotionReceipts; runtime feature → qualifying Evidence linkage; disable/rollback when qualifying Evidence is invalidated.

## Exit gate
The system can propose, test, reject, replicate, and promote mechanisms without allowing the proposing Actor or experiment to make itself authoritative.

---

# H12 — Mature AI-Native Institutional Platform

## Objective
Integrate proven layers while preserving their separations.

## Target architecture
```text
                         Mission / Human Authority
                                  │
                                  ▼
                          Organization Plane
                                  │
                ┌─────────────────┼────────────────┐
                ▼                 ▼                ▼
          Epistemic Plane    Disposition Plane  Authority Plane
                └─────────────────┼────────────────┘
                                  ▼
                            Program Plane
                                  │
                                  ▼
                            Actor Plane
                                  │
                                  ▼
                         Capability Plane
                         /               \
                        ▼                 ▼
                   Access / Use       Production
                        \                 /
                         └───────┬─────────┘
                                 ▼
                            Execution

Surrounding planes:
  provenance • event history • audit • scientific governance • observability
```

## Mature platform properties
Model neutrality; human + AI organizational participation; durable missions/Programs; policy/Authority transparency; CapabilityEvidence/routing; bounded local/remote execution; institutional memory; Evidence-backed reasoning; Disposition separation; capability development/reproduction; federation; scientific mechanism governance; sustainability/ecological accounting.

## Non-goal even at maturity
The platform should resist becoming one omniscient global brain. Authority, cognition, Evidence, local environments, and Organizations remain separable.

---

# 4. Cross-horizon research tracks

## R1 — Context and memory
What should remain exact versus summarized? When does institutional process outperform memory preprocessing? How should stale recalled Evidence be reclassified?

## R2 — Authority and safety
Capability ontology; Authority attenuation; one-shot approvals; remote execution receipts; secret handling; containment boundaries.

## R3 — Durable autonomous work
Long-running Program revision; concurrent work; currentness receipts; Verification freshness; effect reconciliation.

## R4 — Organizational intelligence
Team composition; structured synthesis; turnover resilience; institutional memory; organizational causal history.

## R5 — Capability science
Capability measurement; portability; composition; reproduction; regeneration; source opportunity cost.

## R6 — Scientific governance
Mechanism registries; experimental integrity; promotion criteria; negative results; architecture-as-promoted-claim.

Research may start before its production horizon but remains non-binding until promoted.

---

# 5. Productization sequence

```text
H1: developer interface + tests
H2: local autonomous worker
H3: long-horizon workbench
H4: governed capability ecosystem
H5: team workspace
H6-H7: organization / institution console
H8: capability registry / composer
H9: development / reproduction laboratory
H10: federation console
H11-H12: scientific / institutional governance platform
```

---

# 6. Scaling triggers

Add a durable work queue only when multiple independent work units require asynchronous scheduling beyond one Host loop.

Add distributed workers only when one machine/process cannot satisfy an evidenced workload, isolation, or locality requirement.

Add a server-grade shared datastore only when local-store writer, availability, or concurrency limits become measured blockers.

Add specialized semantic-retrieval infrastructure only when structured retrieval and ordinary indexing fail a defined recall benchmark.

Add multi-Actor concurrency only when single-Actor decomposition is a measured bottleneck and Authority/recovery semantics are stable.

Add remote execution only when a real Capability requires a different trust boundary, operating environment, data locality, hardware, or network location.

Add federation only when at least two independently useful environments have a concrete capability-sharing need.

---

# 7. Programme gates that must never be bypassed

| New power | Required control before promotion |
|---|---|
| Model action | Typed Capability + Host Authority |
| Mutating effect | Operation/effect journal + reconciliation |
| Long-horizon memory | Durable exact source + bounded context + provenance |
| Dynamic extensions | trusted-kernel boundary + lifecycle + admission |
| Multi-Actor execution | delegation envelope + stale-owner fencing + independent completion |
| Organization | durable event/role/mission state + Authority lineage |
| Capability market | CapabilityEvidence + scope + source cost |
| Reproduction | prospective developmental contract + independent evaluation |
| Federation | sovereignty + explicit rights + ecological accounting |
| Self-improvement | independent scientific acceptance + promotion gate |

---

# 8. What should remain lightweight throughout

Prefer semantic precision over infrastructure volume:

- one canonical owner per authoritative state;
- plain schemas over opaque framework objects;
- local storage as long as sufficient;
- explicit adapters over protocol leakage;
- normal code over premature workflow languages;
- Evidence references over copied narrative;
- deterministic gates around probabilistic cognition;
- bounded optional modules rather than universal services;
- experiments outside the trusted kernel until promoted.

---

# 9. Strategic checkpoints

Review programme architecture before freezing each horizon; after substantial architecture research synthesis; after incidents/evaluations expose reusable controls; before new Authority classes; before remote/distributed execution; before production multi-Actor work; before capability reproduction promotion; before federation or autonomous institutional adaptation.

The Architecture Pattern Register is architecture memory and comparison surface, not execution backlog.

---

# 10. Long-term definition of success

> **A mission can persist across model and member turnover; work can execute through governed Capabilities; every consequential action can be tied to explicit Authority and Evidence; Organizations can preserve useful institutional state; capabilities can be evaluated and composed; and the system can eventually learn how to produce and reproduce useful capability rather than merely reallocating scarce intelligence.**

The defining property is not Actor count or model sophistication. It is **durable, governed, evidence-backed capability formation and execution**.
