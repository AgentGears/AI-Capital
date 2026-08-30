from __future__ import annotations

from dataclasses import dataclass

from .enums import (
    ActorStatus,
    AuthorityDecisionKind,
    ClaimStatus,
    CompletionResult,
    ContextCompleteness,
    DispositionKind,
    EffectClass,
    EffectStatus,
    ExecutionOutcome,
    ModelAttemptOutcome,
    ProgramStatus,
    ReconciliationStatus,
    Reversibility,
    RiskClass,
    VerificationResult,
    WorkItemStatus,
)
from .frozen_json import FrozenMap, freeze_json


@dataclass(frozen=True, slots=True)
class WorkItem:
    work_item_id: str
    description: str
    status: WorkItemStatus = WorkItemStatus.OPEN


@dataclass(frozen=True, slots=True)
class Program:
    program_id: str
    revision: int
    objective: str
    constraints: tuple[str, ...] = ()
    assumptions: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    work_items: tuple[WorkItem, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    operation_refs: tuple[str, ...] = ()
    verification_refs: tuple[str, ...] = ()
    success_criteria: tuple[str, ...] = ()
    status: ProgramStatus = ProgramStatus.CREATED


@dataclass(frozen=True, slots=True)
class Actor:
    actor_id: str
    generation: int
    profile: str
    model_binding: str
    procedural_memory_refs: tuple[str, ...] = ()
    grant_refs: tuple[str, ...] = ()
    status: ActorStatus = ActorStatus.ACTIVE


@dataclass(frozen=True, slots=True)
class Capability:
    capability_id: str
    schema_version: int
    operation: str
    resource_type: str
    effect_class: EffectClass
    reversibility: Reversibility
    risk_class: RiskClass
    input_schema: FrozenMap
    output_schema: FrozenMap
    binding_revision: int
    handler_binding: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_schema", freeze_json(self.input_schema))
        object.__setattr__(self, "output_schema", freeze_json(self.output_schema))


@dataclass(frozen=True, slots=True)
class Grant:
    grant_id: str
    subject_ref: str
    capability_scope: tuple[str, ...]
    resource_scope: tuple[str, ...]
    effect_ceiling: EffectClass
    constraints: tuple[str, ...]
    issued_at: str
    expires_at: str | None
    revision: int


@dataclass(frozen=True, slots=True)
class AuthorityDecision:
    decision_id: str
    request_id: str
    resolved_effect: str
    decision: AuthorityDecisionKind
    rationale_code: str
    policy_revision: int
    grant_refs: tuple[str, ...]
    decided_at: str


@dataclass(frozen=True, slots=True)
class Operation:
    operation_id: str
    program_id: str
    actor_id: str
    capability_id: str
    authority_receipt_ref: str
    request_digest: str
    execution_outcome: ExecutionOutcome
    effect_status: EffectStatus
    reconciliation_status: ReconciliationStatus
    started_at: str | None = None
    finished_at: str | None = None
    receipt_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Evidence:
    evidence_id: str
    source_class: str
    observed_at: str
    content_ref: str
    digest: str
    provenance: tuple[str, ...]
    trust_class: str
    currentness: str


@dataclass(frozen=True, slots=True)
class Claim:
    claim_id: str
    statement: str
    evidence_refs: tuple[str, ...]
    status: ClaimStatus
    created_at: str


@dataclass(frozen=True, slots=True)
class Disposition:
    disposition_id: str
    subject_ref: str
    evidence_refs: tuple[str, ...]
    assumption_refs: tuple[str, ...]
    decision: DispositionKind
    rationale: str
    policy_revision: int


@dataclass(frozen=True, slots=True)
class Verification:
    verification_id: str
    subject_ref: str
    subject_revision: int
    subject_digest: str
    contract_ref: str
    result: VerificationResult
    evidence_refs: tuple[str, ...]
    performed_at: str


@dataclass(frozen=True, slots=True)
class Event:
    event_id: str
    sequence: int
    event_type: str
    occurred_at: str
    recorded_at: str
    payload: FrozenMap
    digest: str
    actor_id: str | None = None
    program_id: str | None = None
    causation_id: str | None = None
    correlation_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", freeze_json(self.payload))


@dataclass(frozen=True, slots=True)
class ContextReceipt:
    context_receipt_id: str
    program_id: str
    program_revision: int
    included_refs: tuple[str, ...]
    excluded_refs: tuple[str, ...]
    completeness: ContextCompleteness
    budget_units: int
    created_at: str


@dataclass(frozen=True, slots=True)
class InferenceRequest:
    attempt_id: str
    actor_id: str
    actor_generation: int
    program_id: str
    program_revision: int
    model_binding: str
    context_receipt_ref: str
    context: FrozenMap

    def __post_init__(self) -> None:
        object.__setattr__(self, "context", freeze_json(self.context))


@dataclass(frozen=True, slots=True)
class ModelAttemptReceipt:
    attempt_id: str
    actor_id: str
    actor_generation: int
    program_id: str
    program_revision: int
    model_binding: str
    context_receipt_ref: str
    effective_config_digest: str
    outcome: ModelAttemptOutcome
    started_at: str
    finished_at: str
    output_digest: str | None
    error_code: str | None


@dataclass(frozen=True, slots=True)
class ExecutionAuthorityReceipt:
    receipt_id: str
    decision_id: str
    program_id: str
    program_revision: int
    actor_id: str
    actor_generation: int
    capability_id: str
    capability_binding_revision: int
    policy_revision: int
    grant_refs: tuple[str, ...]
    resolved_effect_digest: str
    issued_at: str
    single_use_identity: str


@dataclass(frozen=True, slots=True)
class CompletionReceipt:
    receipt_id: str
    program_id: str
    program_revision: int
    verification_refs: tuple[str, ...]
    operation_refs: tuple[str, ...]
    result: CompletionResult
    rationale_codes: tuple[str, ...]
    certified_at: str


@dataclass(frozen=True, slots=True)
class ReasoningProposal:
    text: str


@dataclass(frozen=True, slots=True)
class CapabilityRequest:
    request_id: str
    capability_id: str
    arguments: FrozenMap
    expected_binding_revision: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", freeze_json(self.arguments))


@dataclass(frozen=True, slots=True)
class ClaimProposal:
    statement: str
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CompletionProposal:
    rationale: str


@dataclass(frozen=True, slots=True)
class ModelTurn:
    provenance_receipt: str
    reasoning_proposals: tuple[ReasoningProposal, ...] = ()
    capability_requests: tuple[CapabilityRequest, ...] = ()
    claim_proposals: tuple[ClaimProposal, ...] = ()
    completion_proposal: CompletionProposal | None = None
