from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_capital.kernel.context import ContextCompiler, ContextRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ContextPriority
from ai_capital.kernel.errors import IntegrityViolation
from ai_capital.kernel.models import Program


class K8ReviewRound42Tests(unittest.TestCase):
    def test_late_host_control_prevents_stale_receipt_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(
                    Program("p-1", 0, "receipt-time Host-control freshness")
                )
                contexts = ContextRepository(programs)
                compiler = ContextCompiler(contexts)
                original_current_program_source = contexts.current_program_source
                injected = False

                def current_program_source_with_late_control(*args, **kwargs):
                    nonlocal injected
                    source = original_current_program_source(*args, **kwargs)
                    if not injected:
                        injected = True
                        contexts.persist_source(
                            program.program_id,
                            priority=ContextPriority.HOST_CONTROL,
                            payload={"rule": "late mandatory control"},
                        )
                    return source

                with patch.object(
                    contexts,
                    "current_program_source",
                    side_effect=current_program_source_with_late_control,
                ):
                    with self.assertRaisesRegex(
                        IntegrityViolation,
                        "stale relative to current Host controls",
                    ):
                        compiler.compile(program.program_id, budget_units=100_000)

                self.assertTrue(injected)
                receipt_count = programs._db.execute(
                    "SELECT COUNT(*) FROM context_receipts"
                ).fetchone()[0]
                compiled_event_count = programs._db.execute(
                    "SELECT COUNT(*) FROM events WHERE event_type = 'context.compiled'"
                ).fetchone()[0]
                self.assertEqual(int(receipt_count), 0)
                self.assertEqual(int(compiled_event_count), 0)
            finally:
                programs.close()

    def test_unchanged_host_control_set_receipts_successfully(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(
                    Program("p-1", 0, "stable receipt-time Host-control freshness")
                )
                contexts = ContextRepository(programs)
                control_ref = contexts.persist_source(
                    program.program_id,
                    priority=ContextPriority.HOST_CONTROL,
                    payload={"rule": "stable mandatory control"},
                )
                compiled = ContextCompiler(contexts).compile(
                    program.program_id,
                    budget_units=100_000,
                )
                self.assertIn(control_ref, compiled.receipt.included_refs)
                durable = contexts.get(compiled.receipt.context_receipt_id)
                self.assertEqual(durable, compiled)
                compiled_event_count = programs._db.execute(
                    "SELECT COUNT(*) FROM events WHERE event_type = 'context.compiled'"
                ).fetchone()[0]
                self.assertEqual(int(compiled_event_count), 1)
            finally:
                programs.close()


if __name__ == "__main__":
    unittest.main()
