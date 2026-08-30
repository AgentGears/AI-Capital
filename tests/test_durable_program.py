from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ProgramStatus, WorkItemStatus
from ai_capital.kernel.errors import (
    IntegrityViolation,
    InvalidRequest,
    PersistenceConflict,
    StaleProgramRevision,
)
from ai_capital.kernel.models import Program, WorkItem


class FaultingRepository(ProgramRepository):
    def __init__(self, database_path, fail_stage=None):
        self.fail_stage = fail_stage
        super().__init__(database_path)

    def _fault(self, stage: str) -> None:
        if stage == self.fail_stage:
            raise RuntimeError(f"injected fault: {stage}")


class DurableProgramTests(unittest.TestCase):
    def test_restart_reconstructs_exact_program_without_context(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "program.db"
            with ProgramRepository(path) as repository:
                program = repository.create(Program("p-1", 0, "durable work"))
                program = repository.transition(
                    "p-1", ProgramStatus.ACTIVE, expected_revision=program.revision
                )
                program = repository.add_work(
                    "p-1", WorkItem("w-1", "perform bounded work"),
                    expected_revision=program.revision,
                )
                program = repository.satisfy_work(
                    "p-1", "w-1", expected_revision=program.revision
                )
                expected = program
                repository.verify_integrity("p-1")
                event_types = tuple(event.event_type for event in repository.list_events("p-1"))
                self.assertEqual(event_types, (
                    "program.created",
                    "program.activated",
                    "program.work_added",
                    "program.work_satisfied",
                ))

            context_projection = {"summary": "disposable"}
            del context_projection

            with ProgramRepository(path) as restarted:
                restored = restarted.get("p-1")
                self.assertEqual(restored, expected)
                self.assertEqual(restored.work_items[0].status, WorkItemStatus.SATISFIED)
                self.assertEqual(restarted.rebuild("p-1"), expected)
                restarted.verify_integrity("p-1")

    def test_stale_revision_write_rejected_without_new_event(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "program.db"
            with ProgramRepository(path) as repository:
                repository.create(Program("p-1", 0, "durable work"))
                repository.transition("p-1", ProgramStatus.ACTIVE, expected_revision=0)
                before = repository.list_events("p-1")
                with self.assertRaises(StaleProgramRevision):
                    repository.add_work(
                        "p-1", WorkItem("w-1", "stale"), expected_revision=0
                    )
                self.assertEqual(repository.list_events("p-1"), before)

    def test_fault_after_event_append_rolls_back_event_and_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "program.db"
            repository = FaultingRepository(path, fail_stage="after_event_append")
            try:
                with self.assertRaises(RuntimeError):
                    repository.create(Program("p-1", 0, "atomic"), event_id="e-1")
                self.assertEqual(repository.list_events("p-1"), ())
                with self.assertRaises(InvalidRequest):
                    repository.get("p-1")
            finally:
                repository.close()

    def test_fault_during_mutation_preserves_prior_committed_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "program.db"
            repository = FaultingRepository(path)
            try:
                original = repository.create(Program("p-1", 0, "atomic"))
                repository.fail_stage = "after_event_append"
                with self.assertRaises(RuntimeError):
                    repository.transition("p-1", ProgramStatus.ACTIVE, expected_revision=0)
                self.assertEqual(repository.get("p-1"), original)
                self.assertEqual(len(repository.list_events("p-1")), 1)
            finally:
                repository.close()

    def test_duplicate_event_identity_rolls_back_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "program.db"
            with ProgramRepository(path) as repository:
                original = repository.create(
                    Program("p-1", 0, "dedupe"), event_id="same-event"
                )
                with self.assertRaises(PersistenceConflict):
                    repository.transition(
                        "p-1", ProgramStatus.ACTIVE,
                        expected_revision=0, event_id="same-event",
                    )
                self.assertEqual(repository.get("p-1"), original)
                self.assertEqual(len(repository.list_events("p-1")), 1)

    def test_projection_corruption_is_detected_and_repairable_from_events(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "program.db"
            with ProgramRepository(path) as repository:
                repository.create(Program("p-1", 0, "repair"))
                repository.transition("p-1", ProgramStatus.ACTIVE, expected_revision=0)
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "UPDATE program_projections SET projection_json = ? WHERE program_id = ?",
                    ('{"corrupt":true}', "p-1"),
                )
                connection.commit()
            finally:
                connection.close()

            with ProgramRepository(path) as repository:
                with self.assertRaises(IntegrityViolation):
                    repository.get("p-1")
                repaired = repository.repair_projection("p-1")
                self.assertEqual(repaired.status, ProgramStatus.ACTIVE)
                self.assertEqual(repaired.revision, 1)
                repository.verify_integrity("p-1")

    def test_event_corruption_blocks_rebuild(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "program.db"
            with ProgramRepository(path) as repository:
                repository.create(Program("p-1", 0, "event integrity"))
            connection = sqlite3.connect(path)
            try:
                row = connection.execute(
                    "SELECT event_json FROM events WHERE program_id = ?",
                    ("p-1",),
                ).fetchone()
                corrupted = row[0].replace("program.created", "program.failed")
                connection.execute(
                    "UPDATE events SET event_json = ? WHERE program_id = ?",
                    (corrupted, "p-1"),
                )
                connection.commit()
            finally:
                connection.close()

            with ProgramRepository(path) as repository:
                with self.assertRaises(IntegrityViolation):
                    repository.rebuild("p-1")

    def test_second_program_writer_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "program.db"
            first = ProgramRepository(path)
            try:
                with self.assertRaises(PersistenceConflict):
                    ProgramRepository(path)
            finally:
                first.close()

    def test_newer_store_schema_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "program.db"
            connection = sqlite3.connect(path)
            try:
                connection.execute("PRAGMA user_version = 99")
                connection.commit()
            finally:
                connection.close()
            with self.assertRaises(IntegrityViolation):
                ProgramRepository(path)


if __name__ == "__main__":
    unittest.main()
