# AI Capital Architecture Constitution

**Document class:** Constitutional architecture authority  
**Programme:** AI Capital  
**Document version:** 0.1  
**Status:** K0 architecture-freeze candidate  
**Prepared:** 2026-08-30

---

# 1. Purpose

This document defines the architectural constitution of AI Capital. It establishes the concepts, invariants, ownership boundaries, authority rules, truth classes, lifecycle semantics, and change-control requirements that all authoritative AI Capital implementation must preserve.

This document is intentionally stronger than an implementation plan. Roadmaps may schedule work, pattern registers may preserve alternatives, and implementation may change repeatedly, but none may silently weaken a constitutional invariant.

AI Capital begins as a small autonomous-work kernel and may later grow into a substrate for durable AI-native organizations and institutions. Growth is permitted only when new power is accompanied by an explicit control mechanism and executable evidence that the control works.

The architectural objective is:

> **Turn flexible machine cognition into durable, governed, evidence-backed capability without allowing cognition, memory, execution mechanics, or presentation state to become hidden authority.**

---

# 2. Project boundary and vocabulary rule

AI Capital owns its architectural vocabulary.

Repository-facing architecture must be expressed only in AI Capital-native or vendor-neutral terms. External product names, project names, branded abstractions, comparative references, and source-specific vocabulary do not belong in the canonical AI Capital architecture documents.

External research may inform design privately, but anything admitted into AI Capital must be restated as an independent AI Capital concept and justified under AI Capital's own invariants, failure model, adoption decision, and executable evidence.

Canonical rule:

> **External research may inform AI Capital; external identities do not define AI Capital.**

The canonical project documents are:

```text
ARCHITECTURE_CONSTITUTION.md
AI_CAPITAL_AUTONOMOUS_WORK_KERNEL_ROADMAP.md
AI_CAPITAL_MASTER_ROADMAP.md
AI_CAPITAL_ARCHITECTURE_PATTERN_REGISTER.md
```

Document authority order:

```text
Architecture Constitution
        ↓
Master Roadmap
        ↓
Autonomous Work Kernel Roadmap
        ↓
Architecture Pattern Register
        ↓
Implementation plans / issues / code / tests
```

The Architecture Pattern Register is non-executing. It cannot authorize implementation or supersede the Constitution by itself.

---

# 3. Project identity

**AI Capital** uses “Capital” in the sense of a principal center, seat, or leading locus.

Core interpretation:

> **AI Capital is the center where artificial intelligence becomes durable capability.**

The system is not defined by one model, one agent framework, one tool protocol, one workflow engine, or one deployment topology. The system is defined by durable semantic contracts that remain meaningful as those implementation choices change.

---

# 4. Architectural scope

The first production scope is a small autonomous-work kernel with:

```text
one Host authority boundary
one durable Program authority
one active Actor per Program
one local durable store
one workspace
one append-oriented event history
one small typed capability set
one model interface
one deterministic authority evaluator
one independent completion certifier
```

Initial non-goals include distributed scheduling, multi-Actor swarms, autonomous organizational role creation, capability markets, self-modifying policy, autonomous architecture promotion, broad extension ecosystems, remote execution without demonstrated need, universal knowledge graphs, universal memory systems, and infrastructure introduced only for anticipated scale.

These are deferrals, not permanent prohibitions.

---

# 5. Constitutional design equations

```text
Model ≠ Actor
Conversation ≠ Program
Actor ≠ Organization
Tool ≠ Capability
Capability availability ≠ Permission
Permission ≠ Exposure
Exposure ≠ Invocation
Invocation ≠ Effect confirmation
Evidence ≠ Claim
Claim ≠ Belief
Belief ≠ Policy
Policy ≠ Authority
Conclusion ≠ Disposition
Disposition ≠ Authority
Execution outcome ≠ Environmental effect
Verification passed ≠ Verification still current
Agent completion proposal ≠ Program completion
Historical exactness ≠ Current authority
Retention ≠ Context residency
Visibility ≠ Authorization
Access ≠ Ownership
Ownership ≠ Migration
Migration ≠ Reproduction
Capability transfer ≠ Capability reproduction
Capability consumption ≠ Capability creation
Organization success ≠ Ecosystem success
Evidence generation ≠ Scientific acceptance
Containment ≠ Authorization
```

No implementation may collapse two sides of these distinctions without an explicit constitutional amendment.

---

# 6. Constitutional principles

## C-01 — Long-lived intent is durable
Long-running work is represented by durable Program state. Conversation, model context, transient plans, and worker-local state are never canonical task authority.

## C-02 — Short-lived cognition is replaceable
A model invocation is disposable cognition. Replacing a model or provider must not replace Program identity, Actor identity, authority state, effect truth, Evidence, or historical receipts.

## C-03 — Cognition proposes; trusted mechanisms decide
Model output may propose reasoning, Claims, Capabilities, or completion. It cannot directly authorize protected execution, write canonical Program state, admit Evidence, or declare terminal success.

## C-04 — Authority is explicit
Protected effects require explicit current authority. Authority cannot be inferred from capability availability, model confidence, memory, role names, prior approval text, or historical access.

## C-05 — Effect truth is independent from call success
A failed, cancelled, or timed-out execution may still have produced an environmental effect. Unknown effect state remains explicit until reconciled or permanently unresolved.

## C-06 — Evidence remains distinguishable from interpretation
Source-bearing Evidence and semantic Claims are separate objects. Memory, summaries, or model assertions cannot silently become Evidence.

## C-07 — Completion is independently certified
A model or Actor may propose completion. The Host certifies completion only from canonical current state, current Verification, required work, and Operation/effect truth.

## C-08 — Context is a projection, not storage
The model sees a bounded projection over durable state. Context eviction changes residency, not historical existence. Exact history required for audit or recovery is persisted independently.

## C-09 — New power requires corresponding control
No new power class enters production before its control class is implemented and verified.

## C-10 — Complexity must be earned
Additional infrastructure, concurrency, distribution, extension mechanisms, and federation require a demonstrated forcing function.

---

# 7. Canonical domain objects

K0 establishes these first-class objects.

## 7.1 Program
```text
Program {
  program_id
  revision
  objective
  constraints[]
  assumptions[]
  decisions[]
  work_items[]
  evidence_refs[]
  operation_refs[]
  verification_refs[]
  success_criteria[]
  status
}
```
The Program owns what work currently exists, what is satisfied, what remains outstanding, and what defines terminal success. It does not own environmental effect truth, Evidence bytes, policy semantics, or model state.

## 7.2 Actor
```text
Actor {
  actor_id
  generation
  profile
  model_binding
  procedural_memory_refs[]
  grant_refs[]
  status
}
```
Actor identity survives model replacement. Actor existence does not imply execution authority.

## 7.3 Capability
```text
Capability {
  capability_id
  schema_version
  operation
  resource_type
  effect_class
  reversibility
  risk_class
  input_schema
  output_schema
  binding_revision
  handler_binding
}
```
Implementation mechanics are subordinate to Capability semantics.

## 7.4 Grant
```text
Grant {
  grant_id
  subject_ref
  capability_scope
  resource_scope
  effect_ceiling
  constraints[]
  issued_at
  expires_at?
  revision
}
```
A Grant does not itself prove that one proposed execution is current or admissible.

## 7.5 AuthorityDecision
```text
AuthorityDecision {
  decision_id
  request_id
  resolved_effect
  decision: allow | ask | deny
  rationale_code
  policy_revision
  grant_refs[]
  decided_at
}
```

## 7.6 Operation
```text
Operation {
  operation_id
  program_id
  actor_id
  capability_id
  authority_receipt_ref
  request_digest
  execution_outcome
  effect_status
  reconciliation_status
  started_at
  finished_at?
  receipt_refs[]
}
```

## 7.7 Evidence
```text
Evidence {
  evidence_id
  source_class
  observed_at
  content_ref
  digest
  provenance
  trust_class
  currentness
}
```

## 7.8 Claim
```text
Claim {
  claim_id
  statement
  evidence_refs[]
  status: proposed | supported | contradicted | superseded
  created_at
}
```

## 7.9 Disposition
```text
Disposition {
  disposition_id
  subject_ref
  evidence_refs[]
  assumption_refs[]
  decision: proceed | proceed_with_warning | request_evidence | hold | block
  rationale
  policy_revision
}
```
Disposition does not replace execution authority.

## 7.10 Verification
```text
Verification {
  verification_id
  subject_ref
  subject_revision
  subject_digest
  contract_ref
  result: pass | fail | indeterminate
  evidence_refs[]
  performed_at
}
```

## 7.11 Event
```text
Event {
  event_id
  sequence
  event_type
  occurred_at
  recorded_at
  actor_id?
  program_id?
  causation_id?
  correlation_id?
  payload
  digest
}
```
The event ledger is historical truth. It is not automatically current Program state or environmental effect authority.

---

# 8. Authoritative ownership matrix

| State / decision | Canonical owner | Non-authoritative participants |
|---|---|---|
| Program lifecycle and revision | Program Host | Actor, UI, projections |
| Actor identity/generation | Program Host | model adapter |
| Capability definition/binding | Capability Registry under Host control | adapters, extensions |
| Grant state | Authority subsystem | Actor, UI |
| Concrete effect resolution | Capability Broker / Host | model proposal |
| Authority decision | Authority subsystem | model, adapter |
| Operation identity/lifecycle | Operation Journal | handler, adapter |
| Environmental effect status | Operation Journal + reconciler | model narration |
| Evidence admission | Evidence subsystem | Actor proposal |
| Claim state | Epistemic subsystem | Actor proposal |
| Disposition | Disposition subsystem | semantic inference |
| Verification receipt | Verification subsystem | Actor proposal |
| Program completion | Completion Oracle under Host authority | Actor completion proposal |
| Event append | Host event writer | projections/readers |
| Context projection | Context compiler | model |

No subsystem may independently author another subsystem's canonical state through shared mutable objects.

---

# 9. Trust boundary

Trusted responsibilities are the minimum mechanisms required to preserve canonical work, authority, effect truth, Evidence admission, Verification currentness, and completion:

```text
Program Host
Capability Registry / Broker
Authority subsystem
Operation Journal / Reconciliation
Evidence admission
Verification
Completion Oracle
Durable event writer
Canonical storage adapters
```

Advisory or non-authoritative-by-itself inputs include model output, free-form user content, retrieved memory, external content, extension metadata, handler self-report, UI state, cached projections, summaries, and historical narrative.

---

# 10. Model-output contract

```text
ModelTurn {
  reasoning_proposals[]
  capability_requests[]
  claim_proposals[]
  completion_proposal?
  provenance_receipt
}
```

Model output cannot directly encode canonical Program mutations, authority Grants, approval consumption, Operation effect truth, Evidence admission status, Verification authority, or terminal Program state. Such content is treated as a proposal or untrusted text.

---

# 11. Capability semantics

```text
EffectClass = observe | create | modify | delete | external_side_effect
Reversibility = reversible | compensatable | irreversible | unknown
RiskClass = low | medium | high
```

Capability lifecycle states remain distinct:

```text
available
admitted
granted
exposed
requested
authorized
invoked
effect_confirmed
```

No earlier state implies a later state.

---

# 12. Fresh execution authority

A minimum execution-authority receipt binds:

```text
program_id
program_revision
actor_id
actor_generation
capability_id
capability_binding_revision
policy_revision
grant_refs[]
resolved_effect_digest
issued_at
single_use_identity
```

Stale authority fails before handler dispatch. Currentness is not permission; both must pass.

---

# 13. Program state machine

```text
created
active
blocked
completion_pending
completed
failed
cancelled
```

Allowed conceptual transitions:

```text
created → active
created → cancelled
active → blocked
active → completion_pending
active → failed
active → cancelled
blocked → active
blocked → failed
blocked → cancelled
completion_pending → active
completion_pending → blocked
completion_pending → completed
completion_pending → failed
completion_pending → cancelled
```

`completed`, `failed`, and `cancelled` are terminal under v0.1. `completion_pending` is not success.

---

# 14. Operation semantic dimensions

```text
ExecutionOutcome = not_started | running | succeeded | failed | cancelled | timed_out
EffectStatus = unknown | confirmed | absent | indeterminate | not_applicable
ReconciliationStatus = not_required | pending | resolved | unresolved
```

The system must never infer `absent` merely because execution failed, timed out, or returned no acknowledgement.

---

# 15. Retry rule

Protected mutation may be retried only when at least one is proven:

1. the prior effect is confirmed absent;
2. the execution mechanism provides a Host-bound idempotency contract covering the exact operation;
3. reconciliation has established a safe successor operation.

`indeterminate` is never equivalent to retry-safe.

---

# 16. Evidence and epistemic hierarchy

```text
External reality
   ↓ observation
Observation
   ↓ admission
Evidence
   ↓ interpretation
Claim
   ↓ adoption by a cognitive participant
Belief
   ↓ governance process
Policy
   ↓ grant / authorization
Authority
   ↓ consequential action posture
Disposition
   ↓ execution admission
Operation
```

This expresses dependency, not automatic promotion. No transition is implicit.

---

# 17. Disposition boundary

AI Capital preserves two distinct questions:

```text
Semantic question: What follows from the available information?
Disposition question: Given that conclusion, Evidence, policy, and uncertainty, what posture may we take?
```

A correct conclusion may yield `request_evidence`, `hold`, or `block`. A permissive Disposition still does not grant execution authority.

---

# 18. Verification freshness

Verification is indexed to subject state. A Verification receipt must identify the revision/digest/Evidence root required to establish what was verified.

> **Passing Verification is valid only for the state it proves.**

Semantically relevant change invalidates or requires re-evaluation of dependent Verification.

---

# 19. Completion constitution

The Actor may propose completion. Only the Completion Oracle may certify it.

```text
complete iff
  success criteria are satisfied
  AND required work is satisfied
  AND mandatory Verification is current and passing
  AND no unresolved completion blocker exists
  AND no protected mutating Operation is outstanding
  AND no protected effect remains indeterminate where certainty is required
  AND Program currentness is valid
```

Model says done, Actor idle, plan exhaustion, conversation end, execution success, visible work checks, or stale Verification never imply completion.

---

# 20. Context constitution

```text
Durable exact history
        ↓
index / navigation
        ↓
context compiler
        ↓
bounded model-visible context
```

Rules: persist required exact material before eviction; eviction changes residency, not existence; recalled history retains provenance and historical currentness; recall never restores expired authority; ContextReceipts record inclusion/omission; recall remains bounded; summaries remain advisory projections unless separately admitted as Evidence.

---

# 21. Event constitution

The event stream is append-oriented, has one authoritative writer per local canonical sequence, immutable identities, deterministic local ordering, causation/correlation where relevant, schema versioning, digest protection, and rebuildable projections where required.

The event ledger is not a substitute for explicit Program current state or Operation effect status.

---

# 22. Projection rule

Mutable presentation is permitted over immutable or append-oriented receipts. A projection may optimize for reading, lag, be rebuilt, or be discarded, but must identify source currentness where consequential and must never become the only audit source.

---

# 23. Error taxonomy

```text
InvalidRequest
InvalidStateTransition
StaleProgramRevision
StaleActorGeneration
UnknownCapability
StaleCapabilityBinding
CapabilityUnavailable
AuthorityDenied
ApprovalRequired
ApprovalInvalid
ApprovalConsumed
ExecutionFailure
ExecutionTimeout
ExecutionCancelled
EffectIndeterminate
ReconciliationRequired
ReconciliationFailed
EvidenceInvalid
EvidenceMissing
VerificationFailed
VerificationIndeterminate
VerificationStale
CompletionBlocked
ContextIncomplete
ContextBudgetExceeded
PersistenceConflict
IntegrityViolation
InternalFault
```

Errors remain distinct where recovery differs.

---

# 24. Persistence constitution

The first kernel uses one local durable transactional store plus a filesystem-like artifact/workspace boundary. Architecture must not depend on one storage product.

Required properties: atomic canonical transactions where needed; deterministic schema migration; revision conflict detection; append-oriented event durability; Operation identity surviving restart; Evidence content addressing; backup/export before production use; corruption detection for protected records.

More complex persistence topology requires a demonstrated forcing function.

---

# 25. Process constitution

```text
one Host process
one active Program execution loop
one active Actor per Program
zero distributed workers
zero mandatory message brokers
```

Actor cognition may initially run in-process but must cross the same logical Host authority interfaces used under future isolation. Co-location never implies ambient authority.

---

# 26. Containment constitution

Containment is defense in depth, not authorization. Protected execution requires both authorization and containment appropriate to the risk profile.

---

# 27. Extension constitution

Early AI Capital uses explicit interfaces/modules rather than a general dynamic extension platform. If extension lifecycle is later introduced, extensions register below trusted Host boundaries; availability never implies admission/grant; unload/replacement revokes owned registrations; active Operations retain reconciliation ownership; ordinary extensions cannot replace Program, Authority, Operation, Verification, Evidence-admission, or Completion ownership.

---

# 28. Multi-Actor constitutional constraints

Before production multi-Actor execution: child work has Host-owned durable identity; Actor activation is replaceable; delegation authority attenuates; stale Actors are fenced; protected effects preserve Operation truth; parent integration is explicit; child self-report does not certify parent completion.

---

# 29. Organization constitutional constraints

An Organization is durable state beyond current Actors. It must not be represented merely as a shared conversation, role prompt, global memory, or Actor list. Durable mission, membership, roles, policy, authority lineage, Programs, and institutional history remain explicit.

---

# 30. Capability-development constitutional constraints

```text
Capability claim ≠ Capability Evidence
Capability transfer ≠ Capability reproduction
Raw stock growth ≠ Productive regeneration
Destination gain ≠ Ecosystem gain
```

Capability reproduction requires a fresh instance produced under an explicit developmental contract and independently evaluated without prohibited source-private state.

---

# 31. Federation constitutional constraints

> **Federation must not destroy local sovereignty.**

A local environment remains useful when disconnected. Cross-environment access, authority, Evidence, ownership, migration, reproduction, and composition are explicit rights/contracts rather than one generic share action.

---

# 32. Scientific-governance constitutional constraints

```text
Evidence generation ≠ Acceptance
Proposal ≠ Promotion
Self-evaluation ≠ Independent qualification
Self-improvement ≠ Self-granted authority
```

A mechanism cannot make itself authoritative merely by generating Evidence about itself.

---

# 33. Architecture Pattern Register constitution

The Architecture Pattern Register is durable architecture memory.

> **Observation is not adoption. Adoption is not implementation. Implementation is not verification.**

It may preserve accepted patterns, candidates, deferrals, rejections, anti-patterns, implementation lessons, incident-derived controls, research hypotheses, and syntheses. It may not silently create implementation scope.

---

# 34. Architecture-change control

A constitutional change requires an explicit architecture decision with:

```text
change_id
proposal
constitutional sections affected
forcing_function
failure_model
migration_impact
security_impact
durability_impact
evidence_impact
compatibility_analysis
required_executable_proof
rollback_or_supersession_plan
authority
decision_date
```

A roadmap edit, implementation convenience, or model-generated suggestion is insufficient to amend the Constitution.

---

# 35. Pattern-promotion control

A pattern may alter implementation only with: a real forcing function; invariant compatibility; exact scope; explicit adaptation; failure model; executable proof plan; explicit authorization; implementation linkage.

External popularity, conceptual elegance, or implementation elsewhere does not count as project authority.

---

# 36. K0 executable proof contract

## K0-P1 — Deterministic schemas
Canonical domain objects serialize and deserialize deterministically under declared schema versions.

## K0-P2 — Illegal transitions reject
Program and Operation semantic transition violations fail closed.

## K0-P3 — Model independence
No canonical domain object requires a specific model-provider identity or external protocol type.

## K0-P4 — Canonical ownership
No model adapter, capability handler, projection, or UI path can directly author Program canonical state outside Host APIs.

## K0-P5 — Authority boundary
A Capability cannot execute a protected effect without a current AuthorityDecision and ExecutionAuthorityReceipt.

## K0-P6 — Effect uncertainty survives
An ambiguous fault remains `indeterminate` and is not automatically converted to `absent` or replayed.

## K0-P7 — Evidence distinction
An unverified model statement or memory record cannot be persisted as verified Evidence without explicit admission.

## K0-P8 — Completion independence
A completion proposal cannot set Program status to `completed` without Completion Oracle certification.

## K0-P9 — Context non-authority
Removing model context does not delete canonical Program, Operation, Evidence, or Event history.

## K0-P10 — Vocabulary hygiene
Canonical architecture documents contain only AI Capital-native or vendor-neutral architecture terminology and no external project/product references.

---

# 37. K0 architecture-freeze checklist

- [ ] Constitution reviewed and accepted.
- [ ] Three companion documents normalized against it.
- [ ] Domain schemas exist as executable types.
- [ ] Program transition tests exist.
- [ ] Operation semantic tests exist.
- [ ] Authority ownership tests exist.
- [ ] Error taxonomy is encoded.
- [ ] Repository vocabulary scan passes.
- [ ] No unresolved contradiction exists among current accepted architecture patterns.
- [ ] No K1 implementation relies on deferred H2+ concepts.

---

# 38. Constitutional bottom line

AI Capital should remain understandable as a set of durable contracts rather than a pile of agent features.

> **Durable work survives cognition; authority constrains execution; effect truth survives failure; Evidence remains traceable; context remains bounded without destroying history; and completion is certified independently from the Actor that proposes it.**
