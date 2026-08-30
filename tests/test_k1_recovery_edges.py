from dataclasses import replace
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ProgramStatus
from ai_capital.kernel.errors import IntegrityViolation, PersistenceConflict, StaleProgramRevision
from ai_capital.kernel.events import event_digest_fields
from ai_capital.kernel.models import Event, Program
from ai_capital.kernel.schema_codec import record_to_json


class FaultingRepository(ProgramRepository):
    def __init__(self, database_path, fail_stage=None):
        self.fail_stage = fail_stage
        super().__init__(database_path)

    def _fault(self, stage: str) -> None:
        if stage == self.fail_stage:
            raise RuntimeError(f"injected fault: {stage}")


def _recompute(event: Event, *, event_type: str) -> Event:
    changed = replace(event, event_type=event_type, digest="")
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


class K1RecoveryEdgeTests(unittest.TestCase):
    def test_create_commit_survives_lost_acknowledgement(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "program.db"
            repository = FaultingRepository(path, fail_stage="after_commit")
            try:
                with self.assertRaises(RuntimeError):
                    repository.create(
                        Program("p-1", 0, "committed despite lost acknowledgement"),
                        event_id="e-create",
                    )
            finally:
                repository.close()

            with ProgramRepository(path) as recovered:
                program = recovered.get("p-1")
                self.assertEqual(program.revision, 0)
                self.assertEqual(program.status, ProgramStatus.CREATED)
                events = recovered.list_events("p-1")
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0].event_id, "e-create")
                recovered.verify_integrity("p-1")

    def test_mutation_commit_survives_lost_acknowledgement(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "program.db"
            with ProgramRepository(path) as repository:
                repository.create(Program("p-1", 0, "mutation acknowledgement"))

            repository = FaultingRepository(path, fail_stage="after_commit")
            try:
                with self.assertRaises(RuntimeError):
                    repository.transition(
                        "p-1",
                        ProgramStatus.ACTIVE,
                        expected_revision=0,
                        event_id="e-active",
                    )
            finally:
                repository.close()

            with ProgramRepository(path) as recovered:
                program = recovered.get("p-1")
                self.assertEqual(program.revision, 1)
                self.assertEqual(program.status, ProgramStatus.ACTIVE)
                self.assertEqual(recovered.list_events("p-1")[-1].event_id, "e-active")
                recovered.verify_integrity("p-1")

    def test_orphan_history_prevents_program_identity_recreation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "program.db"
            with ProgramRepository(path) as repository:
                repository.create(Program("p-1", 0, "orphan history"))

            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "DELETE FROM program_projections WHERE program_id = ?",
                    ("p-1",),
                )
                connection.commit()
            finally:
                connection.close()

            with ProgramRepository(path) as repository:
                with self.assertRaises(PersistenceConflict):
                    repository.create(Program("p-1", 0, "replacement"))
                repaired = repository.repair_projection("p-1")
                self.assertEqual(repaired.objective, "orphan history")
                repository.verify_integrity("p-1")

    def test_semantically_forged_event_is_rejected_even_with_valid_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "program.db"
            with ProgramRepository(path) as repository:
                repository.create(Program("p-1", 0, "semantic integrity"))
                repository.transition("p-1", ProgramStatus.ACTIVE, expected_revision=0)
                event = repository.list_events("p-1")[-1]

            forged = _recompute(event, event_type="program.work_added")
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    """
                    UPDATE events
                    SET event_type = ?, event_json = ?, event_digest = ?
                    WHERE event_id = ?
                    """,
                    (
                        forged.event_type,
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

    def test_stale_revision_wins_over_invalid_target_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "program.db"
            with ProgramRepository(path) as repository:
                repository.create(Program("p-1", 0, "stale precedence"))
                repository.transition("p-1", ProgramStatus.ACTIVE, expected_revision=0)
                with self.assertRaises(StaleProgramRevision):
                    repository.transition(
                        "p-1",
                        ProgramStatus.COMPLETED,
                        expected_revision=0,
                    )


if __name__ == "__main__":
    unittest.main()
