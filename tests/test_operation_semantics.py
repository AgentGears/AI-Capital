from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_capital.kernel.enums import EffectStatus, ExecutionOutcome, ReconciliationStatus
from ai_capital.kernel.errors import IntegrityViolation
from ai_capital.kernel.models import Operation
from ai_capital.kernel.operations import retry_is_safe, validate_operation_semantics


def make_operation(outcome, effect, reconciliation):
    return Operation(
        operation_id="op-1",
        program_id="p-1",
        actor_id="a-1",
        capability_id="c-1",
        authority_receipt_ref="auth-1",
        request_digest="digest",
        execution_outcome=outcome,
        effect_status=effect,
        reconciliation_status=reconciliation,
    )


class OperationSemanticTests(unittest.TestCase):
    def test_running_keeps_unknown_effect(self):
        validate_operation_semantics(make_operation(
            ExecutionOutcome.RUNNING,
            EffectStatus.UNKNOWN,
            ReconciliationStatus.NOT_REQUIRED,
        ))

    def test_indeterminate_timeout_is_not_retry_safe(self):
        operation = make_operation(
            ExecutionOutcome.TIMED_OUT,
            EffectStatus.INDETERMINATE,
            ReconciliationStatus.PENDING,
        )
        validate_operation_semantics(operation)
        self.assertFalse(retry_is_safe(operation))

    def test_absent_effect_is_retry_safe(self):
        operation = make_operation(
            ExecutionOutcome.FAILED,
            EffectStatus.ABSENT,
            ReconciliationStatus.RESOLVED,
        )
        self.assertTrue(retry_is_safe(operation))

    def test_terminal_unknown_effect_rejected(self):
        with self.assertRaises(IntegrityViolation):
            validate_operation_semantics(make_operation(
                ExecutionOutcome.TIMED_OUT,
                EffectStatus.UNKNOWN,
                ReconciliationStatus.PENDING,
            ))


if __name__ == "__main__":
    unittest.main()
