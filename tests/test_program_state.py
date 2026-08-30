from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_capital.kernel.enums import ProgramStatus
from ai_capital.kernel.errors import InvalidStateTransition, StaleProgramRevision
from ai_capital.kernel.models import Program
from ai_capital.kernel.program_state import transition_program


class ProgramStateTests(unittest.TestCase):
    def test_revision_fenced_transition(self):
        program = Program("p-1", 0, "prove K0")
        active = transition_program(program, ProgramStatus.ACTIVE, expected_revision=0)
        self.assertEqual(active.revision, 1)
        with self.assertRaises(StaleProgramRevision):
            transition_program(active, ProgramStatus.BLOCKED, expected_revision=0)

    def test_illegal_transition_rejected(self):
        program = Program("p-1", 0, "prove K0")
        with self.assertRaises(InvalidStateTransition):
            transition_program(program, ProgramStatus.COMPLETED, expected_revision=0)

    def test_completion_pending_is_not_completion(self):
        program = Program("p-1", 0, "prove K0")
        active = transition_program(program, ProgramStatus.ACTIVE, expected_revision=0)
        pending = transition_program(active, ProgramStatus.COMPLETION_PENDING, expected_revision=1)
        self.assertNotEqual(pending.status, ProgramStatus.COMPLETED)
        completed = transition_program(pending, ProgramStatus.COMPLETED, expected_revision=2)
        self.assertEqual(completed.status, ProgramStatus.COMPLETED)


if __name__ == "__main__":
    unittest.main()
