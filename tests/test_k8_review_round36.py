from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from ai_capital.kernel.context import ContextRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ContextPriority
from ai_capital.kernel.models import Program


_PROJECTION_COLUMNS = (
    "sequence", "event_id", "program_id", "program_revision", "priority",
    "source_digest", "payload_units", "payload_json", "event_digest",
    "projection_digest",
)


class K8ReviewRound36Tests(unittest.TestCase):
    def _setup_control(self, database: Path):
        programs = ProgramRepository(database)
        program = programs.create(
            Program("p-1", 0, "authenticated Host-control selector migration")
        )
        contexts = ContextRepository(programs)
        source_ref = contexts.persist_source(
            program.program_id,
            priority=ContextPriority.HOST_CONTROL,
            payload={"rule": "mandatory"},
        )
        return programs, program, source_ref.removeprefix("event:")

    def test_v6_migration_marks_every_host_control_selector_divergence(self):
        mutations = (
            ("context_source_program_id", "p-mutated"),
            ("context_source_program_revision", 9),
            ("context_source_priority", ContextPriority.ADVISORY_MEMORY.value),
        )
        for column, value in mutations:
            with self.subTest(column=column), tempfile.TemporaryDirectory() as directory:
                database = Path(directory) / "host.db"
                programs, _program, event_id = self._setup_control(database)
                with programs._transaction():
                    programs._db.execute(
                        f"UPDATE events SET {column} = ? WHERE event_id = ?",
                        (value, event_id),
                    )
                    programs._db.execute(
                        "DELETE FROM context_persisted_source_invalidations "
                        "WHERE event_id = ?",
                        (event_id,),
                    )
                    programs._db.execute(
                        "UPDATE component_schema SET version = 6 "
                        "WHERE component = 'bounded_context'"
                    )
                programs.close()

                reopened = ProgramRepository(database)
                try:
                    ContextRepository(reopened)
                    marker = reopened._db.execute(
                        "SELECT event_id FROM context_persisted_source_invalidations "
                        "WHERE event_id = ?",
                        (event_id,),
                    ).fetchone()
                    self.assertIsNotNone(marker)
                finally:
                    reopened.close()

    def test_v6_reconstruction_marks_semantic_event_digest_divergence(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "host.db"
            programs, _program, event_id = self._setup_control(database)
            projection = programs._db.execute(
                "SELECT " + ", ".join(_PROJECTION_COLUMNS) +
                " FROM context_persisted_source_index WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            assert projection is not None
            projection_values = tuple(projection[column] for column in _PROJECTION_COLUMNS)

            with programs._transaction():
                programs._db.execute(
                    "UPDATE events SET event_digest = ? WHERE event_id = ?",
                    ("0" * 64, event_id),
                )
                # A v6 store can contain preserved authenticated projection evidence
                # even when the semantic selector tuple has diverged. Restore that exact
                # authenticated prior row after the current trigger models invalidation.
                programs._db.execute(
                    "INSERT OR REPLACE INTO context_persisted_source_index(" +
                    ", ".join(_PROJECTION_COLUMNS) +
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    projection_values,
                )
                programs._db.execute(
                    "DELETE FROM context_persisted_source_invalidations "
                    "WHERE event_id = ?",
                    (event_id,),
                )

            repository = object.__new__(ContextRepository)
            repository._host_store = programs
            repository._evidence = None
            with programs._transaction():
                repository._migrate_host_control_invalidations()

            marker = programs._db.execute(
                "SELECT event_id FROM context_persisted_source_invalidations "
                "WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            self.assertIsNotNone(marker)
            programs.close()


if __name__ == "__main__":
    unittest.main()
