from .enums import EffectStatus, ExecutionOutcome, ReconciliationStatus
from .errors import IntegrityViolation
from .models import Operation


_TERMINAL_OUTCOMES = frozenset({
    ExecutionOutcome.SUCCEEDED,
    ExecutionOutcome.FAILED,
    ExecutionOutcome.CANCELLED,
    ExecutionOutcome.TIMED_OUT,
})


def validate_operation_semantics(operation: Operation) -> None:
    outcome = operation.execution_outcome
    effect = operation.effect_status
    reconciliation = operation.reconciliation_status

    if outcome in {ExecutionOutcome.NOT_STARTED, ExecutionOutcome.RUNNING}:
        if effect is not EffectStatus.UNKNOWN:
            raise IntegrityViolation("non-terminal execution must retain unknown effect status")
        if reconciliation is not ReconciliationStatus.NOT_REQUIRED:
            raise IntegrityViolation("reconciliation cannot start before terminal execution")
        return

    if outcome not in _TERMINAL_OUTCOMES:
        raise IntegrityViolation(f"unsupported execution outcome: {outcome}")

    if effect is EffectStatus.UNKNOWN:
        raise IntegrityViolation("terminal execution cannot retain unknown effect status")

    if effect is EffectStatus.INDETERMINATE:
        if reconciliation not in {
            ReconciliationStatus.PENDING,
            ReconciliationStatus.UNRESOLVED,
        }:
            raise IntegrityViolation(
                "indeterminate effect requires pending or unresolved reconciliation"
            )
        return

    if reconciliation not in {
        ReconciliationStatus.NOT_REQUIRED,
        ReconciliationStatus.RESOLVED,
    }:
        raise IntegrityViolation(
            "determinate effect requires not_required or resolved reconciliation"
        )

    if outcome is ExecutionOutcome.SUCCEEDED and effect is EffectStatus.ABSENT:
        raise IntegrityViolation("successful execution cannot assert absent effect")


def retry_is_safe(operation: Operation) -> bool:
    """K0 only recognizes proven absence as intrinsically retry-safe.

    Host-validated idempotency contracts belong to the later Operation admission
    layer and must not be represented as an untrusted boolean bypass here.
    """
    validate_operation_semantics(operation)
    return operation.effect_status is EffectStatus.ABSENT
