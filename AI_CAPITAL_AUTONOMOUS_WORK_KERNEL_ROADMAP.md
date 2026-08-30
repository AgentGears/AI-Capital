# AI Capital — Autonomous Work Kernel Roadmap

**Document class:** Implementation roadmap  
**Programme:** AI Capital  
**Document version:** 0.1  
**Status:** K0 architecture-freeze candidate  
**Prepared:** 2026-08-30  
**Scope:** Small, model-neutral autonomous-work kernel  
**Constitutional authority:** `ARCHITECTURE_CONSTITUTION.md`

---

# 1. Objective

Build the smallest credible runtime for durable autonomous work in which:

- a **Program**, not a conversation, is canonical work state;
- an **Actor**, not a model invocation, is the persistent cognitive identity;
- cognition proposes actions but does not own protected execution authority;
- executable mechanisms are exposed as typed **Capabilities**;
- environmental effects have explicit outcome, uncertainty, and reconciliation truth;
- important Claims resolve to provenance-bearing **Evidence**;
- terminal completion is independently certified by the Host;
- exact history is retained independently from bounded model context;
- the first usable system remains one Host, one local durable store, one workspace, and one active Actor unless a later forcing function requires more.

The kernel is not an operating system for every form of AI work, a marketplace, a multi-Actor society, a general workflow platform, or an enterprise control plane. It is the trusted substrate on which later AI Capital horizons may build.

---

# 2. Constitutional invariants

This roadmap is subordinate to `ARCHITECTURE_CONSTITUTION.md`.

```text
Model ≠ Actor
Conversation ≠ Program
Tool ≠ Capability
Capability availability ≠ Permission
Permission ≠ Invocation
Invocation ≠ Effect confirmation
Evidence ≠ Authority
Evidence ≠ Claim
Conclusion ≠ Disposition
Disposition ≠ Authority
Execution outcome ≠ Environmental effect
Verification passed ≠ Verification still current
Agent completion proposal ≠ Program completion
Historical exactness ≠ Current authority
Retention ≠ Context residency
Containment ≠ Authorization
```

Runtime rules:

1. **Host authority:** canonical admission, Program transition, capability authorization, Operation truth, recovery, Evidence admission, Verification currentness, and terminal certification are Host-owned.
2. **Single canonical Program:** no transcript, plan, task list, model session, extension, or worker-local graph may become parallel work authority.
3. **Typed action boundary:** model output requests declared Capabilities; it does not invoke arbitrary Host functions.
4. **Fail-visible uncertainty:** unknown effect state remains explicit.
5. **Provenance before positive claims:** verification-critical Claims resolve to admitted Evidence.
6. **Bounded context, durable history:** context reduction changes residency, not historical existence.
7. **Adapter neutrality:** external implementation vocabulary terminates at adapters; canonical objects remain AI Capital-owned.
8. **No complexity without a forcing function:** distribution, multi-Actor concurrency, generic extension platforms, remote execution, and specialized retrieval infrastructure remain outside the kernel until demonstrated need exists.

---

# 3. Minimum object model

K0 freezes canonical schemas for:

```text
Program
Actor
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
ModelAttemptReceipt
ExecutionAuthorityReceipt
CompletionReceipt
```

Detailed semantics live in `ARCHITECTURE_CONSTITUTION.md`.

---

# 4. Reference runtime shape

```text
User / API / CLI
       │
       ▼
┌────────────────────┐
│    Program Host    │  canonical work + lifecycle authority
└─────────┬──────────┘
          │ bounded context
          ▼
┌────────────────────┐
│       Actor        │  replaceable cognition
└─────────┬──────────┘
          │ proposals
          ▼
┌────────────────────┐
│ Capability Broker  │  resolve capability + concrete effect
└──────┬───────┬─────┘
       │       │
       ▼       ▼
 Authority   Evidence / Disposition
       │       │
       └───┬───┘
           ▼
     Operation Journal
           │
           ▼
       Execution
           │
           ▼
 Verify / Reconcile / Record
           │
           └──────────────► Program Host

Around all layers: append-oriented Event Ledger
```

---

# 5. Kernel sequence

```text
K0  Constitution, contracts, state machines, proof harness
K1  Durable Program + append-oriented event state
K2  Replaceable Actor + model-neutral inference boundary
K3  Typed capability registry + broker
K4  Authority, policy, currentness, approval
K5  Operation journal + effect uncertainty + reconciliation
K6  Evidence, Claims, provenance
K7  Verification + independent completion
K8  Bounded context + durable exact history
K9  End-to-end kernel qualification
```

Each gate closes only through executable proof.

---

# K0 — Constitution, contracts, state machines, and proof harness

## Outcome
Freeze semantic boundaries before implementing autonomous cognition.

## Deliverables
- `ARCHITECTURE_CONSTITUTION.md`;
- canonical schemas for all K0 domain objects;
- Program state machine;
- Operation semantic dimensions and transition rules;
- error taxonomy;
- authoritative ownership matrix;
- repository skeleton;
- deterministic fixture/test harness;
- vocabulary-hygiene test;
- Architecture Pattern Register linked as a non-executing companion.

## Canonical Program statuses
```text
created
active
blocked
completion_pending
completed
failed
cancelled
```

## Canonical Operation dimensions
```text
ExecutionOutcome = not_started | running | succeeded | failed | cancelled | timed_out
EffectStatus = unknown | confirmed | absent | indeterminate | not_applicable
ReconciliationStatus = not_required | pending | resolved | unresolved
```

## K0 proof gate
- schemas serialize/deserialize deterministically;
- illegal Program transitions reject;
- illegal Operation semantic transitions/combinations reject where forbidden;
- canonical objects contain no model-provider-specific types;
- model adapters and capability handlers cannot directly mutate Program canonical state;
- protected execution requires current AuthorityDecision + ExecutionAuthorityReceipt;
- ambiguous mutation remains indeterminate rather than retry-safe;
- model/memory text cannot become verified Evidence without admission;
- model completion cannot set Program terminal state;
- context deletion cannot delete durable truth;
- canonical documentation passes the external-identity vocabulary rule.

## Explicit non-goals
No production model calls, no product UI, no memory learning, no multi-Actor execution, no remote workers.

---

# K1 — Durable Program state and append-oriented history

## Outcome
A Program survives process death and can be reconstructed without a conversation transcript.

## Deliverables
- transactional local durable-store schema and migrations;
- Program repository;
- append-oriented event history;
- deterministic Program projections;
- revision/compare-and-set semantics;
- local writer ownership;
- restart/rebuild command;
- immutable Event identities and digests;
- integrity checks for projection/source divergence.

## Minimum Event vocabulary
```text
program.created
program.activated
program.revised
program.work_added
program.work_satisfied
program.blocked
program.unblocked
program.completion_proposed
program.completion_rejected
program.completed
program.failed
program.cancelled
```

## Proof gate K1
1. Create a Program and perform several legal transitions.
2. Kill the process after durable commit at multiple injection points.
3. Restart from durable storage only.
4. Reconstruct the same canonical Program state.
5. Delete conversation/context projections and prove Program truth remains.
6. Reject stale Program revision writes.
7. Rebuild a deliberately corrupted projection.

## Failure cases
partial write; duplicate Event submission; stale revision mutation; corrupt projection; crash between append and projection update; out-of-order replay attempt.

---

# K2 — Replaceable Actor and model-neutral inference boundary

## Outcome
Cognition becomes replaceable while Program continuity remains intact.

## Deliverables
- model interface owned by AI Capital;
- Actor identity and generation;
- Actor-to-model binding;
- structured `ModelTurn` result;
- ContextReceipt;
- ModelAttemptReceipt with effective configuration metadata;
- deterministic/mock cognition implementation;
- one real model adapter only after the mock contract is stable;
- qualification path for an alternate adapter.

## Model output contract
```text
ReasoningProposal
CapabilityRequest
ClaimProposal
CompletionProposal
```

No model output is canonical state or execution authorization.

## Proof gate K2
- execute half a Program with model binding A;
- replace the Actor model binding/generation;
- continue without changing Program identity/history;
- reject malformed model output without corrupting canonical state;
- prove adapter-native IDs never become Program, Actor, Operation, Event, Grant, or idempotency identities;
- prove a model swap cannot widen authority automatically.

---

# K3 — Typed capability registry and broker

## Outcome
The Actor reasons over semantic Capabilities rather than arbitrary implementation functions.

## Deliverables
- Capability Registry;
- versioned input/output schemas;
- effect/reversibility/risk classes;
- handler binding + binding revision;
- capability snapshot per model turn;
- request validation;
- concrete effect resolver;
- initial built-in capability family.

## Initial capability family
```text
workspace.read
workspace.list
workspace.write
workspace.patch
command.observe
network.fetch
artifact.write
```

Capabilities with broad mutation, external communication, credential use, package installation, interactive environment control, or remote execution remain outside K3 unless separately authorized under K4/K5 semantics.

## Proof gate K3
- unknown capability fails loud;
- invalid input fails before handler dispatch;
- changed binding revision invalidates stale requests;
- availability is observable without implying permission;
- capability metadata is model-neutral;
- handler substitution preserves semantic contract;
- model sees only the capability snapshot included in its ContextReceipt.

---

# K4 — Authority, policy, currentness, and approval boundary

## Outcome
The model can request an effect but cannot grant itself authority.

## Deliverables
- Grant store;
- deterministic policy evaluator;
- resource-scope matching;
- effect ceilings;
- `allow | ask | deny` AuthorityDecision;
- ExecutionAuthorityReceipt binding current Program/Actor/capability/policy/grant state;
- one-shot approval receipt when a concrete use case requires it;
- authority decision Events;
- policy revision identity;
- revocation/currentness checks before dispatch.

## Decision pipeline
```text
CapabilityRequest
      ↓
Resolve concrete target/effect
      ↓
Validate current Program / Actor / capability binding
      ↓
Resolve matching Grants
      ↓
Evaluate policy
      ↓
allow | ask | deny
      ↓
Issue single-use execution authority
      ↓
Operation admission
```

## Proof gate K4
- model cannot call handler directly;
- capability presence does not imply permission;
- stale Program/Actor/capability/policy identity rejects execution;
- one-shot approval cannot authorize a different effect;
- consumed approval cannot be reused;
- `deny` produces no environmental execution;
- audit receipt explains deterministic decision inputs;
- revocation blocks future admission without rewriting history.

---

# K5 — Operation journal, effect uncertainty, and reconciliation

## Outcome
The kernel reports what it knows about environmental effects instead of inferring effect truth from function return status.

## Deliverables
- Host-generated durable Operation identity;
- requested/admitted/started/finished/reconciled semantic Events;
- ExecutionOutcome separate from EffectStatus;
- explicit `indeterminate` effect state;
- reconciliation interface;
- adapter-level idempotency mapped into Host-owned Operation truth;
- no automatic replay of ambiguous mutation;
- Operation receipts linked to Program, Authority, and Evidence.

## Proof gate K5
1. success + confirmed effect;
2. explicit failure + absent effect;
3. timeout + confirmed effect found during reconciliation;
4. timeout + absent effect found during reconciliation;
5. timeout + permanently indeterminate effect;
6. process death after dispatch but before acknowledgement;
7. cancellation after effect but before result;
8. duplicate dispatch carrying same Host idempotency identity.

Ambiguous protected mutation must not be blindly replayed.

---

# K6 — Evidence, Claims, provenance, and Disposition inputs

## Outcome
Important conclusions resolve to bounded source Evidence, and advisory memory/model text cannot silently become proof.

## Deliverables
- Evidence store;
- content-addressed artifacts/digests;
- Claim records;
- Claim-to-Evidence links;
- trust/currentness metadata;
- contradiction/supersession state;
- provenance chain fields;
- explicit Evidence admission mechanism;
- initial Disposition evaluation inputs;
- model-visible Evidence references rather than copied opaque summaries where practical.

## Proof gate K6
- every verification-critical positive Claim resolves to admitted Evidence;
- changing source bytes changes Evidence digest;
- unsupported and supported Claims remain distinguishable;
- historical Evidence remains historical after retrieval;
- memory/model text cannot be marked verified Evidence without explicit admission;
- contradicted/superseded Claims retain history;
- Disposition cannot silently convert Claim confidence into execution authority.

---

# K7 — Verification and independent completion certification

## Outcome
The Actor may propose completion, but only the Host can certify the Program complete.

## Deliverables
- Verification contracts;
- Verification receipt bound to subject revision/digest;
- blocker model;
- Completion Oracle;
- CompletionReceipt;
- explicit rejection reasons;
- Verification invalidation/currentness logic;
- completion decision Events.

## Minimum completion predicate
```text
complete iff
  success criteria satisfied
  AND required work satisfied
  AND mandatory Verification current and passing
  AND no unresolved completion blocker
  AND no outstanding protected mutating Operation
  AND no protected effect remains indeterminate where certainty is required
  AND Program currentness valid
```

## Proof gate K7
- model saying done with outstanding work is rejected;
- stale Verification cannot certify newer Program state;
- unresolved indeterminate effect blocks completion when required;
- replacement Actor cannot bypass checks;
- completion reconstructs from canonical records without transcript interpretation;
- CompletionReceipt identifies exact Program revision and Verification roots.

---

# K8 — Bounded context projection and restart-safe history

## Outcome
Live model context becomes a bounded view over durable Program/history/Evidence, not the storage substrate.

## Deliverables
- deterministic context compiler;
- ContextReceipt listing included/excluded sources;
- priority classes: Host control, current Program, current Evidence, recent interaction, recalled history, advisory memory;
- size-budget enforcement;
- persist-before-evict;
- stable addresses for non-resident exact history;
- bounded recall API;
- completeness/truncation classes.

## Proof gate K8
- old material leaves model context without disappearing from durable history;
- recall after restart returns exact stored source;
- summary/projection loss does not destroy canonical history;
- recalled historical facts do not automatically become current observations or permissions;
- ContextReceipt proves what Evidence/control state was shown to an inference;
- bounded recall cannot bypass context budget.

---

# K9 — End-to-end kernel qualification and v0.1 release gate

## Q1 — Crash continuity
Recover multi-step Programs across injected process death without duplicated protected effects or lost canonical work state.

## Q2 — Model replacement
Switch model binding mid-Program and continue from canonical state.

## Q3 — Authority resistance
Attempt to induce scope escalation; Host denies independently of model obedience.

## Q4 — Ambiguous mutation
Simulate timeout after possible mutation; record `indeterminate`, reconcile, and prove no blind retry.

## Q5 — Evidence trace
Trace CompletionReceipt → Verification → Claim → Evidence → source digest.

## Q6 — False completion
Actor claims success before criteria are met; completion fails deterministically.

## Q7 — Context pressure
History exceeds inference budget; eviction/recall preserves exact Evidence and currentness classification.

## Q8 — Stale authority
Replace Actor generation or capability binding after proposal; stale request cannot execute.

## Q9 — Vocabulary hygiene
Scan four canonical architecture documents and prove they contain only AI Capital-native or vendor-neutral terminology.

## v0.1 release condition
K9 closes only when Q1–Q9 have executable regression tests and the Architecture Pattern Register contains no unresolved contradiction against current accepted constitutional patterns.

---

# 6. Storage and process architecture for v0.1

## 6.1 Process model
```text
1 Host process
1 active Program execution loop
1 active Actor per Program
0 distributed workers
0 mandatory message brokers
```

Process separation for cognition is optional initially but remains an architectural seam. In-process cognition still uses Host authorization APIs.

## 6.2 Persistence classes
Use one local durable transactional store for:
```text
programs
program_work_items
actors
capabilities
grants
authority_decisions
execution_authority_receipts
operations
events
evidence
claims
verifications
dispositions
context_receipts
model_attempt_receipts
completion_receipts
```

Use a filesystem-like boundary for:
```text
workspace/
artifacts/
evidence/
skills/
```

Do not introduce specialized retrieval or distributed storage infrastructure in v0.1 without a measured forcing function.

## 6.3 Suggested repository layout
```text
src/
  ai_capital/
    kernel/
      program.py
      actor.py
      events.py
      storage.py
      context.py
      verification.py
      completion.py
    capability/
      types.py
      registry.py
      broker.py
      builtins/
    authority/
      grants.py
      policy.py
      approval.py
      receipts.py
    operations/
      types.py
      journal.py
      reconciliation.py
    evidence/
      evidence.py
      claims.py
      provenance.py
    cognition/
      base.py
      mock.py
    disposition.py
policies/
skills/
workspace/
tests/
docs/
```

---

# 7. Test strategy

1. **Pure domain tests** — schemas, state machines, policy decisions.
2. **Persistence tests** — crash/restart, migration, conflict, rebuild.
3. **Fault-injection tests** — timeout, process death, partial acknowledgement, handler exception.
4. **Authority tests** — scope escalation, stale Grants, stale Actor generation, stale capability binding, approval replay.
5. **Cognition-boundary tests** — malformed response, model replacement, duplicate proposal identity.
6. **Evidence tests** — digest mismatch, broken source reference, stale Evidence, incomplete projection.
7. **Completion tests** — stale Verification, blockers, indeterminate effects, outstanding work.
8. **Context tests** — persist-before-evict, exact recall, currentness reclassification, bounded recall.
9. **End-to-end scenarios** — realistic Programs under deterministic failure injection.
10. **Architecture hygiene tests** — canonical terminology and prohibited dependency checks.

The kernel should contain more executable failure-path tests than prompt-quality tests. Prompt quality is tunable; Authority, durability, Evidence, effect truth, and completion are architectural.

---

# 8. Explicit deferrals after v0.1

Do not add multi-Actor swarms, distributed scheduling, remote workers, capability marketplaces, tokenized economies/bidding, autonomous role invention, self-modifying runtime/policy, universal knowledge graphs, autonomous memory consolidation, generic workflow languages, generic extension marketplaces, capability reproduction, Organization/federation runtime layers, anticipated distributed infrastructure, or specialized retrieval infrastructure without a failing benchmark.

Each belongs to a later AI Capital horizon with its own forcing function and proof gate.

---

# 9. Kernel exit criteria

The autonomous-work kernel is architecturally closed when:

- Program truth survives restart and context loss;
- Actor model binding is replaceable without losing work identity;
- Capability invocation cannot bypass Host Authority;
- availability, permission, exposure, invocation, and effect confirmation are distinct;
- ExecutionOutcome and environmental EffectStatus are distinct;
- ambiguous mutation cannot be blindly replayed;
- material Claims link to provenance-bearing Evidence;
- bounded context can omit history without deleting exact history;
- completion is Host-certified from current canonical state and Verification;
- the single-Host/local-store architecture remains sufficient for demonstrated workload;
- accepted architecture patterns have executable proof or explicit non-implementation rationale.

At that point AI Capital may progress from **kernel correctness** to **system intelligence and breadth**.
