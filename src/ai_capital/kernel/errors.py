class AICapitalError(Exception):
    """Base class for kernel contract failures."""


class InvalidRequest(AICapitalError): pass
class InvalidStateTransition(AICapitalError): pass
class StaleProgramRevision(AICapitalError): pass
class StaleActorGeneration(AICapitalError): pass
class UnknownCapability(AICapitalError): pass
class StaleCapabilityBinding(AICapitalError): pass
class CapabilityUnavailable(AICapitalError): pass
class AuthorityDenied(AICapitalError): pass
class ApprovalRequired(AICapitalError): pass
class ApprovalInvalid(AICapitalError): pass
class ApprovalConsumed(AICapitalError): pass
class ExecutionFailure(AICapitalError): pass
class ExecutionTimeout(AICapitalError): pass
class ExecutionCancelled(AICapitalError): pass
class EffectIndeterminate(AICapitalError): pass
class ReconciliationRequired(AICapitalError): pass
class ReconciliationFailed(AICapitalError): pass
class EvidenceInvalid(AICapitalError): pass
class EvidenceMissing(AICapitalError): pass
class VerificationFailed(AICapitalError): pass
class VerificationIndeterminate(AICapitalError): pass
class VerificationStale(AICapitalError): pass
class CompletionBlocked(AICapitalError): pass
class ContextIncomplete(AICapitalError): pass
class ContextBudgetExceeded(AICapitalError): pass
class PersistenceConflict(AICapitalError): pass
class IntegrityViolation(AICapitalError): pass
class InternalFault(AICapitalError): pass
