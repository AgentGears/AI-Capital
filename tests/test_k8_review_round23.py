from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_capital.kernel.context import ContextCompiler, ContextRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ContextPriority
from ai_capital.kernel.models import Program


class K8ReviewRound23Tests(unittest.TestCase):
    def test_recall_materialization_is_clamped_to_context_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "bounded recall clamp"))
                contexts = ContextRepository(programs)
                compiler = ContextCompiler(contexts)
                baseline = compiler.compile(program.program_id, budget_units=100000)
                recalled_ref = contexts.persist_source(
                    program.program_id,
                    priority=ContextPriority.ADVISORY_MEMORY,
                    payload={"memory": "x" * 100000},
                )
                with patch.object(
                    contexts,
                    "_event_by_id",
                    side_effect=AssertionError("over-budget recalled Event was decoded"),
                ) as event_by_id:
                    compiled = compiler.compile(
                        program.program_id,
                        budget_units=baseline.used_units,
                        recalled_refs=(recalled_ref,),
                        recall_max_units=200000,
                    )
                event_by_id.assert_not_called()
                self.assertIn(recalled_ref, compiled.receipt.excluded_refs)
            finally:
                programs.close()

    def test_long_accepted_program_id_fits_authenticated_event_envelope(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program_id = "p" * 8192
                program = programs.create(Program(program_id, 0, "long durable identity"))
                compiled = ContextCompiler(ContextRepository(programs)).compile(
                    program.program_id,
                    budget_units=100000,
                )
                self.assertGreater(compiled.used_units, 0)
            finally:
                programs.close()


if __name__ == "__main__":
    unittest.main()
