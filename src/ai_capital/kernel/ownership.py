from .enums import AuthorityDecisionKind, AuthorityDomain
from .errors import (
    AuthorityDenied,
    IntegrityViolation,
    StaleActorGeneration,
    StaleCapabilityBinding,
    StaleProgramRevision,
)
from .models import Actor, AuthorityDecision, Capability, ExecutionAuthorityReceipt, Program


_CANONICAL_OWNERS: dict[AuthorityDomain, str] = {
    AuthorityDomain.PROGRAM_LIFECYCLE: "ProgramHost",
    AuthorityDomain.ACTOR_IDENTITY: "ProgramHost",
    AuthorityDomain.CAPABILITY_BINDING: "CapabilityRegistry",
    AuthorityDomain.GRANT_STATE: "AuthoritySubsystem",
    AuthorityDomain.EFFECT_RESOLUTION: "CapabilityBroker",
    AuthorityDomain.AUTHORITY_DECISION: "AuthoritySubsystem",
    AuthorityDomain.OPERATION_LIFECYCLE: "OperationJournal",
    AuthorityDomain.EFFECT_STATUS: "OperationJournal",
    AuthorityDomain.EVIDENCE_ADMISSION: "EvidenceSubsystem",
    AuthorityDomain.CLAIM_STATE: "EpistemicSubsystem",
    AuthorityDomain.DISPOSITION: "DispositionSubsystem",
    AuthorityDomain.VERIFICATION: "VerificationSubsystem",
    AuthorityDomain.PROGRAM_COMPLETION: "CompletionOracle",
    AuthorityDomain.EVENT_APPEND: "EventWriter",
    AuthorityDomain.CONTEXT_PROJECTION: "ContextCompiler",
}


def canonical_owner(domain: AuthorityDomain) -> str:
    return _CANONICAL_OWNERS[domain]


def ownership_matrix() -> dict[AuthorityDomain, str]:
    return dict(_CANONICAL_OWNERS)


def validate_execution_authority(
    *,
    receipt: ExecutionAuthorityReceipt,
    decision: AuthorityDecision,
    program: Program,
    actor: Actor,
    capability: Capability,
    policy_revision: int,
) -> None:
    if decision.decision is not AuthorityDecisionKind.ALLOW:
        raise AuthorityDenied("protected execution requires an allow decision")
    if receipt.decision_id != decision.decision_id:
        raise IntegrityViolation("authority receipt is not bound to supplied decision")
    if receipt.program_id != program.program_id or receipt.program_revision != program.revision:
        raise StaleProgramRevision("execution authority is stale for Program")
    if receipt.actor_id != actor.actor_id or receipt.actor_generation != actor.generation:
        raise StaleActorGeneration("execution authority is stale for Actor")
    if (
        receipt.capability_id != capability.capability_id
        or receipt.capability_binding_revision != capability.binding_revision
    ):
        raise StaleCapabilityBinding("execution authority is stale for Capability")
    if receipt.policy_revision != policy_revision or decision.policy_revision != policy_revision:
        raise IntegrityViolation("execution authority is stale for current policy")
    if tuple(receipt.grant_refs) != tuple(decision.grant_refs):
        raise IntegrityViolation("execution authority Grant set differs from decision")
