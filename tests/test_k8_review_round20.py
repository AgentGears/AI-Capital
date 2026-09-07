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


class K8ReviewRound20Tests(unittest.TestCase):
    def test_missing_host_control_projection_row_fails_closed_before_materialization(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "Host control projection coverage"))
                contexts = ContextRepository(programs)
                control_ref = contexts.persist_source(
                    program.program_id,
                    priority=ContextPriority.HOST_CONTROL,
                    payload={"control": "required"},
                )
                programs._db.execute(
                    "DELETE FROM context_persisted_source_index WHERE event_id = ?",
                    (control_ref.removeprefix("event:"),),
                )
                with patch.object(
                    contexts,
                    "_event_by_id",
                    side_effect=AssertionError("missing projection caused Event materialization"),
                ) as event_by_id:
                    with self.assertRaises(IntegrityViolation):
                        ContextCompiler(contexts).compile(
                            program.program_id,
                            budget_units=100_000,
                        )
                event_by_id.assert_not_called()
            finally:
                programs.close()

    def test_complete_host_control_projection_remains_derived(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "Host control projection positive"))
                contexts = ContextRepository(programs)
                control_ref = contexts.persist_source(
                    program.program_id,
                    priority=ContextPriority.HOST_CONTROL,
                    payload={"control": "required"},
                )
                compiled = ContextCompiler(contexts).compile(
                    program.program_id,
                    budget_units=100_000,
                )
                self.assertIn(control_ref, compiled.receipt.included_refs)
            finally:
                programs.close()


if __name__ == "__main__":
    unittest.main()
