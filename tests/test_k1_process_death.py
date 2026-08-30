from dataclasses import replace
from pathlib import Path
import os
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ProgramStatus
from ai_capital.kernel.errors import IntegrityViolation, InvalidRequest
from ai_capital.kernel.events import event_digest_fields
from ai_capital.kernel.models import Event, Program
from ai_capital.kernel.schema_codec import record_to_json


ROOT = Path(__file__).resolve().parents[1]


def _child_environment() -> dict[str, str]:
    environment = dict(os.environ)
    source = str(ROOT / "src")
    existing = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = source if not existing else source + os.pathsep + existing
    return environment


def _run_child(source: str, database_path: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source), str(database_path)],
        cwd=ROOT,
        env=_child_environment(),
        text=True,
        capture_output=True,
        check=False,
    )


def _with_sequence(event: Event, sequence: int) -> Event:
    changed = replace(event, sequence=sequence, digest="")
    digest = event_digest_fields(
        event_id=changed.event_id,
        sequence=changed.sequence,
        event_type=changed.event_type,
        occurred_at=changed.occurred_at,
        recorded_at=changed.recorded_at,
        payload=changed.payload,
        actor_id=changed.actor_id,
        program_id=changed.program_id,
        causation_id=changed.causation_id,
        correlation_id=changed.correlation_id,
    )
    return replace(changed, digest=digest)


class K1ProcessDeathTests(unittest.TestCase):
    def test_process_death_between_event_append_and_projection_rolls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "program.db"
            child = _run_child(
                """
                import os
                import sys
                from ai_capital.kernel.durable_program import ProgramRepository
                from ai_capital.kernel.models import Program

                class CrashRepository(ProgramRepository):
                    def _fault(self, stage):
                        if stage == "after_event_append":
                            os._exit(73)

                repository = CrashRepository(sys.argv[1])
                repository.create(Program("p-1", 0, "die before commit"))
                """,
                path,
            )
            self.assertEqual(child.returncode, 73, child.stderr)

            with ProgramRepository(path) as recovered:
                self.assertEqual(recovered.list_events("p-1"), ())
                with self.assertRaises(InvalidRequest):
                    recovered.get("p-1")

    def test_process_death_after_commit_preserves_committed_transition(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "program.db"
            with ProgramRepository(path) as repository:
                repository.create(Program("p-1", 0, "die after commit"))

            child = _run_child(
                """
                import os
                import sys
                from ai_capital.kernel.durable_program import ProgramRepository
                from ai_capital.kernel.enums import ProgramStatus

                class CrashRepository(ProgramRepository):
                    def _fault(self, stage):
                        if stage == "after_commit":
                            os._exit(74)

                repository = CrashRepository(sys.argv[1])
                repository.transition(
                    "p-1",
                    ProgramStatus.ACTIVE,
                    expected_revision=0,
                    event_id="e-active",
                )
                """,
                path,
            )
            self.assertEqual(child.returncode, 74, child.stderr)

            with ProgramRepository(path) as recovered:
                program = recovered.get("p-1")
                self.assertEqual(program.revision, 1)
                self.assertEqual(program.status, ProgramStatus.ACTIVE)
                self.assertEqual(recovered.list_events("p-1")[-1].event_id, "e-active")
                recovered.verify_integrity("p-1")

    def test_forged_out_of_order_replay_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "program.db"
            with ProgramRepository(path) as repository:
                repository.create(Program("p-1", 0, "ordered history"))
                repository.transition("p-1", ProgramStatus.ACTIVE, expected_revision=0)
                event = repository.list_events("p-1")[-1]

            forged = _with_sequence(event, 0)
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    """
                    UPDATE events
                    SET sequence = ?, event_json = ?, event_digest = ?
                    WHERE event_id = ?
                    """,
                    (
                        forged.sequence,
                        record_to_json(forged),
                        forged.digest,
                        forged.event_id,
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            with ProgramRepository(path) as repository:
                with self.assertRaises(IntegrityViolation):
                    repository.rebuild("p-1")


if __name__ == "__main__":
    unittest.main()
