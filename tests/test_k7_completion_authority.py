from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ProgramStatus
from ai_capital.kernel.errors import InvalidRequest
from ai_capital.kernel.models import Program


class K7CompletionAuthorityTests(unittest.TestCase):
    def test_public_program_transition_cannot_certify_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "kernel.db")
            try:
                program = programs.create(Program("p-1", 0, "completion authority proof"))
                program = programs.transition(
                    "p-1", ProgramStatus.ACTIVE, expected_revision=program.revision
                )
                pending = programs.transition(
                    "p-1",
                    ProgramStatus.COMPLETION_PENDING,
                    expected_revision=program.revision,
                )
                before = programs.list_events("p-1")

                with self.assertRaises(InvalidRequest):
                    programs.transition(
                        "p-1",
                        ProgramStatus.COMPLETED,
                        expected_revision=pending.revision,
                    )

                self.assertEqual(programs.get("p-1"), pending)
                self.assertEqual(programs.list_events("p-1"), before)
                self.assertNotIn(
                    "program.completed",
                    tuple(event.event_type for event in programs.list_events("p-1")),
                )
            finally:
                programs.close()


if __name__ == "__main__":
    unittest.main()
