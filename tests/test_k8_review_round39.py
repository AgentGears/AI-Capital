from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from ai_capital.kernel.context import ContextCompiler, ContextRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.errors import IntegrityViolation
from ai_capital.kernel.models import Program


class K8ReviewRound39Tests(unittest.TestCase):
    @staticmethod
    def _compiled_event_id(
        programs: ProgramRepository,
        context_receipt_id: str,
    ) -> str:
        row = programs._db.execute(
            "SELECT compiled_event_id FROM context_receipts "
            "WHERE context_receipt_id = ?",
            (context_receipt_id,),
        ).fetchone()
        assert row is not None
        return str(row[0])

    def test_compiled_invalidation_survives_loss_of_both_receipt_projections(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "host.db"
            programs = ProgramRepository(database)
            program = programs.create(
                Program("p-1", 0, "durable compiled invalidation evidence")
            )
            contexts = ContextRepository(programs)
            compiled = ContextCompiler(contexts).compile(
                program.program_id,
                budget_units=100000,
            )
            event_id = self._compiled_event_id(
                programs,
                compiled.receipt.context_receipt_id,
            )

            with programs._transaction():
                programs._db.execute(
                    "DELETE FROM context_receipt_event_index WHERE event_id = ?",
                    (event_id,),
                )
                programs._db.execute(
                    "UPDATE events SET event_type = 'context.invalidated' WHERE event_id = ?",
                    (event_id,),
                )
            marker = programs._db.execute(
                "SELECT event_id FROM context_compiled_event_invalidations "
                "WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            self.assertIsNotNone(marker)

            with programs._transaction():
                programs._db.execute(
                    "DELETE FROM context_receipts WHERE compiled_event_id = ?",
                    (event_id,),
                )
            programs.close()

            reopened = ProgramRepository(database)
            try:
                with self.assertRaisesRegex(
                    IntegrityViolation,
                    "compiled Context Event was invalidated",
                ):
                    ContextRepository(reopened)
            finally:
                reopened.close()

    def test_v8_migration_recovers_invalidated_compiled_event_without_receipt_index(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "host.db"
            programs = ProgramRepository(database)
            program = programs.create(
                Program("p-1", 0, "legacy compiled invalidation migration")
            )
            contexts = ContextRepository(programs)
            compiled = ContextCompiler(contexts).compile(
                program.program_id,
                budget_units=100000,
            )
            event_id = self._compiled_event_id(
                programs,
                compiled.receipt.context_receipt_id,
            )

            with programs._transaction():
                programs._db.execute(
                    "DROP TRIGGER IF EXISTS context_recall_event_integrity_invalidate"
                )
                programs._db.execute(
                    "DELETE FROM context_compiled_event_invalidations WHERE event_id = ?",
                    (event_id,),
                )
                programs._db.execute(
                    "DELETE FROM context_receipt_event_index WHERE event_id = ?",
                    (event_id,),
                )
                programs._db.execute(
                    "UPDATE events SET event_type = 'context.invalidated', "
                    "context_recall_invalidated = 1 WHERE event_id = ?",
                    (event_id,),
                )
                programs._db.execute(
                    "UPDATE component_schema SET version = 8 "
                    "WHERE component = 'bounded_context'"
                )
            programs.close()

            reopened = ProgramRepository(database)
            try:
                with self.assertRaisesRegex(
                    IntegrityViolation,
                    "compiled Context Event was invalidated",
                ):
                    ContextRepository(reopened)
            finally:
                reopened.close()


if __name__ == "__main__":
    unittest.main()
