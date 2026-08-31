from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import EffectClass
from ai_capital.kernel.errors import IntegrityViolation
from ai_capital.kernel.models import CapabilityResolution, ResolvedEffect
from ai_capital.kernel.operation_journal import OperationJournal
from ai_capital.kernel.serialization import canonical_digest


class IdempotencyAnchorRemediationTests(unittest.TestCase):
    def test_coordinated_binding_rewrite_cannot_change_backend_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            with ProgramRepository(Path(directory) / "kernel.db") as programs:
                journal = OperationJournal(programs)
                resolution = CapabilityResolution(
                    "req-1",
                    "workspace.write",
                    0,
                    {"path": "a.txt", "content": "x"},
                    ResolvedEffect(
                        "workspace_path",
                        "a.txt",
                        EffectClass.MODIFY,
                        {"content": "x"},
                    ),
                )
                operation = journal.create_intent(
                    program_id="p-1",
                    actor_id="a-1",
                    resolution=resolution,
                    authority_receipt_ref="auth-1",
                    idempotency_key="caller-opt-in",
                )
                binding = programs._db.execute(
                    """
                    SELECT requested_event_id, resolution_digest
                    FROM operation_idempotency_bindings
                    WHERE operation_id = ?
                    """,
                    (operation.operation_id,),
                ).fetchone()
                forged_key = "forged-backend-key"
                forged_binding_digest = canonical_digest(
                    {
                        "idempotency_key": forged_key,
                        "operation_id": operation.operation_id,
                        "resolution_digest": binding["resolution_digest"],
                        "requested_event_id": binding["requested_event_id"],
                    }
                )
                programs._db.execute(
                    """
                    UPDATE operation_projections
                    SET idempotency_key = ?
                    WHERE operation_id = ?
                    """,
                    (forged_key, operation.operation_id),
                )
                programs._db.execute(
                    """
                    UPDATE operation_idempotency_bindings
                    SET idempotency_key = ?, binding_digest = ?
                    WHERE operation_id = ?
                    """,
                    (forged_key, forged_binding_digest, operation.operation_id),
                )

                with self.assertRaises(IntegrityViolation):
                    journal.idempotency_key(operation.operation_id)


if __name__ == "__main__":
    unittest.main()
