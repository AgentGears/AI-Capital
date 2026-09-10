from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.errors import IntegrityViolation
from ai_capital.kernel.events import event_digest_fields
from ai_capital.kernel.evidence_store import EvidenceRepository
from ai_capital.kernel.models import Event, Program
from ai_capital.kernel.schema_codec import record_from_json, record_to_json
from ai_capital.kernel.serialization import to_canonical_data


class K8ReviewRound45Tests(unittest.TestCase):
    @staticmethod
    def _event(programs: ProgramRepository, event_id: str) -> Event:
        row = programs._db.execute(
            "SELECT event_json FROM events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        assert row is not None
        event = record_from_json(Event, row["event_json"])
        assert isinstance(event, Event)
        return event

    @staticmethod
    def _digest_for(event: Event, *, payload: object, program_id: str | None) -> str:
        return event_digest_fields(
            event_id=event.event_id,
            sequence=event.sequence,
            event_type=event.event_type,
            occurred_at=event.occurred_at,
            recorded_at=event.recorded_at,
            payload=payload,
            actor_id=event.actor_id,
            program_id=program_id,
            causation_id=event.causation_id,
            correlation_id=event.correlation_id,
        )

    def test_deployed_v4_reauthenticates_program_bound_admission_event(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "host.db"
            programs = ProgramRepository(database)
            program = programs.create(Program("p-1", 0, "deployed v4 Evidence migration"))
            evidence = EvidenceRepository(programs)
            item = evidence.admit(
                content=b"round-45-evidence",
                source_class="test",
                observed_at="2026-01-01T00:00:00Z",
                provenance=("round-45",),
                trust_class="test",
                currentness="current",
            )
            admitted_event_id = str(evidence._row(item.evidence_id)["admitted_event_id"])
            event = self._event(programs, admitted_event_id)
            payload = to_canonical_data(event.payload)
            digest = self._digest_for(
                event,
                payload=payload,
                program_id=program.program_id,
            )
            forged = replace(
                event,
                program_id=program.program_id,
                payload=payload,
                digest=digest,
            )
            with programs._transaction():
                programs._db.execute(
                    "DROP TRIGGER IF EXISTS evidence_admission_event_integrity_invalidate"
                )
                programs._db.execute(
                    "UPDATE component_schema SET version = 4 "
                    "WHERE component = 'evidence_store'"
                )
                programs._db.execute(
                    "UPDATE events SET program_id = ?, event_json = ?, event_digest = ? "
                    "WHERE event_id = ?",
                    (
                        program.program_id,
                        record_to_json(forged),
                        digest,
                        admitted_event_id,
                    ),
                )
            programs.close()

            reopened = ProgramRepository(database)
            try:
                with self.assertRaisesRegex(
                    IntegrityViolation,
                    "must remain Host-scoped during migration",
                ):
                    EvidenceRepository(reopened)
            finally:
                reopened.close()

    def test_v4_advances_to_v5_and_v5_restart_skips_legacy_rebuild(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "host.db"
            programs = ProgramRepository(database)
            evidence = EvidenceRepository(programs)
            evidence.admit(
                content=b"round-45-clean",
                source_class="test",
                observed_at="2026-01-01T00:00:00Z",
                provenance=("round-45",),
                trust_class="test",
                currentness="current",
            )
            with programs._transaction():
                programs._db.execute(
                    "UPDATE component_schema SET version = 4 "
                    "WHERE component = 'evidence_store'"
                )
            programs.close()

            migrated_host = ProgramRepository(database)
            try:
                EvidenceRepository(migrated_host)
                version = migrated_host._db.execute(
                    "SELECT version FROM component_schema WHERE component = 'evidence_store'"
                ).fetchone()[0]
                self.assertEqual(int(version), 5)
            finally:
                migrated_host.close()

            current_host = ProgramRepository(database)
            try:
                with patch.object(
                    EvidenceRepository,
                    "_rebuild_event_index",
                    side_effect=AssertionError("v5 restart must not rebuild legacy Event index"),
                ):
                    EvidenceRepository(current_host)
            finally:
                current_host.close()


if __name__ == "__main__":
    unittest.main()
