from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import unittest

from ai_capital.kernel.context import ContextRepository, event_ref
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.errors import IntegrityViolation
from ai_capital.kernel.events import event_digest_fields
from ai_capital.kernel.models import Event, Program
from ai_capital.kernel.schema_codec import record_from_json, record_to_json
from ai_capital.kernel.serialization import to_canonical_data


class K8ReviewRound48Tests(unittest.TestCase):
    @staticmethod
    def _coherently_mutate_ordinary_program_event(
        programs: ProgramRepository,
        program_id: str,
    ) -> str:
        row = programs._db.execute(
            """
            SELECT event_id, event_json
            FROM events
            WHERE program_id = ? AND event_type != 'context.source_persisted'
            ORDER BY sequence
            LIMIT 1
            """,
            (program_id,),
        ).fetchone()
        assert row is not None
        event = record_from_json(Event, row["event_json"])
        assert isinstance(event, Event)
        payload = to_canonical_data(event.payload)
        payload["round48_forged_history"] = True
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
            (record_to_json(forged), digest, event.event_id),
        )
        invalidated = programs._db.execute(
            "SELECT context_recall_invalidated FROM events WHERE event_id = ?",
            (event.event_id,),
        ).fetchone()[0]
        assert int(invalidated) == 1
        return event.event_id

    def test_ordinary_event_invalidation_cannot_be_cleared_before_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "host.db"
            programs = ProgramRepository(database)
            program = programs.create(Program("p-1", 0, "Round 48 ordinary Event"))
            ContextRepository(programs)
            event_id = self._coherently_mutate_ordinary_program_event(
                programs,
                program.program_id,
            )
            with self.assertRaisesRegex(
                sqlite3.IntegrityError,
                "Context Event recall invalidation cannot be cleared",
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
            programs.close()

            reopened = ProgramRepository(database)
            try:
                contexts = ContextRepository(reopened)
                with self.assertRaisesRegex(
                    IntegrityViolation,
                    "Event recall source was invalidated",
                ):
                    contexts.recall(
                        program.program_id,
                        source_refs=(event_ref(event_id),),
                        max_items=1,
                        max_units=100000,
                    )
            finally:
                reopened.close()


if __name__ == "__main__":
    unittest.main()
