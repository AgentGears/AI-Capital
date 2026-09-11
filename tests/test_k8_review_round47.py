from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ai_capital.kernel.context import ContextRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ContextPriority
from ai_capital.kernel.errors import IntegrityViolation
from ai_capital.kernel.events import event_digest_fields
from ai_capital.kernel.models import Event, Program
from ai_capital.kernel.schema_codec import record_from_json, record_to_json
from ai_capital.kernel.serialization import canonical_digest, to_canonical_data


class K8ReviewRound47Tests(unittest.TestCase):
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
    def _coherently_mutate_persisted_source(
        programs: ProgramRepository,
        source_ref: str,
    ) -> str:
        event_id = source_ref.removeprefix("event:")
        event = K8ReviewRound47Tests._event(programs, event_id)
        payload = to_canonical_data(event.payload)
        source = payload["source"]
        source["payload"] = {"note": "forged"}
        source["source_digest"] = canonical_digest(source["payload"])
        digest = event_digest_fields(
            event_id=event.event_id,
            sequence=event.sequence,
            event_type=event.event_type,
            occurred_at=event.occurred_at,
            recorded_at=event.recorded_at,
            payload=payload,
            actor_id=event.actor_id,
            program_id=event.program_id,
            causation_id=event.causation_id,
            correlation_id=event.correlation_id,
        )
        forged = replace(event, payload=payload, digest=digest)
        programs._db.execute(
            "UPDATE events SET event_json = ?, event_digest = ? WHERE event_id = ?",
            (record_to_json(forged), digest, event_id),
        )
        invalidated = programs._db.execute(
            "SELECT context_recall_invalidated FROM events WHERE event_id = ?",
            (event_id,),
        ).fetchone()[0]
        assert int(invalidated) == 1
        return event_id

    def test_optional_persisted_source_invalidation_cannot_be_cleared_before_restart(self):
        for priority in (
            ContextPriority.RECENT_INTERACTION,
            ContextPriority.ADVISORY_MEMORY,
        ):
            with self.subTest(priority=priority.value), tempfile.TemporaryDirectory() as directory:
                database = Path(directory) / "host.db"
                programs = ProgramRepository(database)
                program = programs.create(
                    Program("p-1", 0, f"Round 47 marker guard {priority.value}")
                )
                contexts = ContextRepository(programs)
                source_ref = contexts.persist_source(
                    program.program_id,
                    priority=priority,
                    payload={"note": "original"},
                )
                event_id = self._coherently_mutate_persisted_source(
                    programs, source_ref
                )
                with self.assertRaisesRegex(
                    sqlite3.IntegrityError,
                    "persisted Context source invalidation cannot be cleared",
                ):
                    programs._db.execute(
                        "UPDATE events SET context_recall_invalidated = 0 WHERE event_id = ?",
                        (event_id,),
                    )
                marker = programs._db.execute(
                    "SELECT context_recall_invalidated FROM events WHERE event_id = ?",
                    (event_id,),
                ).fetchone()[0]
                self.assertEqual(int(marker), 1)
                programs._db.commit()
                programs.close()

                reopened = ProgramRepository(database)
                try:
                    with self.assertRaisesRegex(
                        IntegrityViolation,
                        "persisted Context source Event was invalidated",
                    ):
                        ContextRepository(reopened)
                finally:
                    reopened.close()

    def test_invalidated_optional_persisted_source_direct_reads_fail_closed(self):
        for priority in (
            ContextPriority.RECENT_INTERACTION,
            ContextPriority.ADVISORY_MEMORY,
        ):
            with self.subTest(priority=priority.value), tempfile.TemporaryDirectory() as directory:
                database = Path(directory) / "host.db"
                programs = ProgramRepository(database)
                try:
                    program = programs.create(
                        Program("p-1", 0, f"Round 47 direct read {priority.value}")
                    )
                    contexts = ContextRepository(programs)
                    source_ref = contexts.persist_source(
                        program.program_id,
                        priority=priority,
                        payload={"note": "original"},
                    )
                    self._coherently_mutate_persisted_source(programs, source_ref)
                    with self.assertRaisesRegex(
                        IntegrityViolation,
                        "persisted Context source Event was invalidated",
                    ):
                        contexts.persisted_source(program.program_id, source_ref)
                    with self.assertRaisesRegex(
                        IntegrityViolation,
                        "persisted Context source Event was invalidated",
                    ):
                        contexts.persisted_source_revision(
                            program.program_id, source_ref
                        )
                finally:
                    programs.close()


if __name__ == "__main__":
    unittest.main()
