from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from ai_capital.kernel.context import ContextRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ContextCompleteness, ContextPriority
from ai_capital.kernel.errors import ContextBudgetExceeded
from ai_capital.kernel.models import Program
from ai_capital.kernel.serialization import canonical_json


class K8ReviewRound3Tests(unittest.TestCase):
    def test_empty_recall_rejects_budget_smaller_than_envelope(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "bounded recall envelope"))
                contexts = ContextRepository(programs)
                with self.assertRaises(ContextBudgetExceeded):
                    contexts.recall(
                        program.program_id,
                        (),
                        max_items=1,
                        max_units=1,
                    )
            finally:
                programs.close()

    def test_nonempty_recall_rejects_budget_smaller_than_envelope(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "bounded recall envelope"))
                contexts = ContextRepository(programs)
                source_ref = contexts.persist_source(
                    program.program_id,
                    priority=ContextPriority.ADVISORY_MEMORY,
                    payload={"memory": "durable"},
                )
                with self.assertRaises(ContextBudgetExceeded):
                    contexts.recall(
                        program.program_id,
                        (source_ref,),
                        max_items=1,
                        max_units=1,
                    )
            finally:
                programs.close()

    def test_empty_recall_accepts_exact_envelope_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "bounded recall envelope"))
                contexts = ContextRepository(programs)
                minimum_units = len(canonical_json({"sources": ()}).encode("utf-8"))
                result = contexts.recall(
                    program.program_id,
                    (),
                    max_items=1,
                    max_units=minimum_units,
                )
                self.assertIs(result.completeness, ContextCompleteness.COMPLETE)
                self.assertEqual(result.used_units, minimum_units)
                self.assertEqual(result.budget_units, minimum_units)
            finally:
                programs.close()


if __name__ == "__main__":
    unittest.main()
