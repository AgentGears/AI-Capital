from enum import Enum


class StringEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class ProgramStatus(StringEnum):
    CREATED = "created"
    ACTIVE = "active"
    BLOCKED = "blocked"
    COMPLETION_PENDING = "completion_pending"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkItemStatus(StringEnum):
    OPEN = "open"
    SATISFIED = "satisfied"


class ActorStatus(StringEnum):
    ACTIVE = "active"
    REPLACED = "replaced"
    DISABLED = "disabled"


class ModelAttemptOutcome(StringEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STALE = "stale"


class EffectClass(StringEnum):
    OBSERVE = "observe"
    CREATE = "create"
    MODIFY = "modify"
    DELETE = "delete"
    EXTERNAL_SIDE_EFFECT = "external_side_effect"


class Reversibility(StringEnum):
    REVERSIBLE = "reversible"
    COMPENSATABLE = "compensatable"
    IRREVERSIBLE = "irreversible"
    UNKNOWN = "unknown"


class RiskClass(StringEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class AuthorityDecisionKind(StringEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class ExecutionOutcome(StringEnum):
    NOT_STARTED = "not_started"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"


class EffectStatus(StringEnum):
    UNKNOWN = "unknown"
    CONFIRMED = "confirmed"
    ABSENT = "absent"
    INDETERMINATE = "indeterminate"
    NOT_APPLICABLE = "not_applicable"


class ReconciliationStatus(StringEnum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"


class ClaimStatus(StringEnum):
    PROPOSED = "proposed"
    SUPPORTED = "supported"
    CONTRADICTED = "contradicted"
    SUPERSEDED = "superseded"


class DispositionKind(StringEnum):
    PROCEED = "proceed"
    PROCEED_WITH_WARNING = "proceed_with_warning"
    REQUEST_EVIDENCE = "request_evidence"
    HOLD = "hold"
    BLOCK = "block"


class VerificationResult(StringEnum):
    PASS = "pass"
    FAIL = "fail"
    INDETERMINATE = "indeterminate"


class CompletionResult(StringEnum):
    CERTIFIED = "certified"
    REJECTED = "rejected"


class ContextCompleteness(StringEnum):
    COMPLETE = "complete"
    TRUNCATED = "truncated"
    INCOMPLETE = "incomplete"


class AuthorityDomain(StringEnum):
    PROGRAM_LIFECYCLE = "program_lifecycle"
    ACTOR_IDENTITY = "actor_identity"
    CAPABILITY_BINDING = "capability_binding"
    GRANT_STATE = "grant_state"
    EFFECT_RESOLUTION = "effect_resolution"
    AUTHORITY_DECISION = "authority_decision"
    OPERATION_LIFECYCLE = "operation_lifecycle"
    EFFECT_STATUS = "effect_status"
    EVIDENCE_ADMISSION = "evidence_admission"
    CLAIM_STATE = "claim_state"
    DISPOSITION = "disposition"
    VERIFICATION = "verification"
    PROGRAM_COMPLETION = "program_completion"
    EVENT_APPEND = "event_append"
    CONTEXT_PROJECTION = "context_projection"
