from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from ai_capital.kernel.context import ContextRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ContextPriority
from ai_capital.kernel.models import Program


class _CursorGuard:
    def __init__(self, cursor, sql: str, forbidden: tuple[str, ...]):
        self._cursor = cursor
        self._sql = sql
        self._forbidden = forbidden

    def fetchall(self):
        if all(fragment in self._sql for fragment in self._forbidden):
            raise AssertionError("unbounded Host-control invalidation migration")
        return self._cursor.fetchall()

    def __iter__(self):
        return iter(self._cursor)

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _FetchallGuardConnection:
    def __init__(self, connection, forbidden: tuple[str, ...]):
        self._connection = connection
        self._forbidden = forbidden

    def execute(self, sql, parameters=()):
        return _CursorGuard(
            self._connection.execute(sql, parameters),
            sql,
            self._forbidden,
        )

    def __getattr__(self, name):
        return getattr(self._connection, name)


class _NoLegacyMigrationConnection:
    def __init__(self, connection, forbidden: tuple[str, ...]):
        self._connection = connection
        self._forbidden = forbidden

    def execute(self, sql, parameters=()):
        if all(fragment in sql for fragment in self._forbidden):
            raise AssertionError(
                "legacy Host-control invalidation reconciliation ran on current schema"
            )
        return self._connection.execute(sql, parameters)

    def __getattr__(self, name):
        return getattr(self._connection, name)


class K8ReviewRound35Tests(unittest.TestCase):
    @staticmethod
    def _event_id(source_ref: str) -> str:
        return source_ref.removeprefix("event:")

    def _setup_controls(self, database: Path):
        programs = ProgramRepository(database)
        program = programs.create(
            Program("p-1", 0, "bounded Host-control invalidation migration")
        )
        contexts = ContextRepository(programs)
        refs = tuple(
            contexts.persist_source(
                program.program_id,
                priority=ContextPriority.HOST_CONTROL,
                payload={"index": index, "rule": "mandatory"},
            )
            for index in range(8)
        )
        return programs, program, refs

    def test_v6_invalidation_reconstruction_streams_projection_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "host.db"
            programs, _program, refs = self._setup_controls(database)
            invalidated_event_id = self._event_id(refs[3])
            with programs._transaction():
                programs._db.execute(
                    "UPDATE events SET event_json = event_json || ' ', "
                    "event_type = 'context.invalidated' WHERE event_id = ?",
                    (invalidated_event_id,),
                )
                programs._db.execute(
                    "DELETE FROM context_persisted_source_invalidations "
                    "WHERE event_id = ?",
                    (invalidated_event_id,),
                )
                programs._db.execute(
                    "UPDATE component_schema SET version = 6 "
                    "WHERE component = 'bounded_context'"
                )
            self.assertIsNotNone(
                programs._db.execute(
                    "SELECT 1 FROM context_persisted_source_index WHERE event_id = ?",
                    (invalidated_event_id,),
                ).fetchone()
            )
            programs.close()

            reopened = ProgramRepository(database)
            original_connection = reopened._connection
            assert original_connection is not None
            reopened._connection = _FetchallGuardConnection(
                original_connection,
                (
                    "FROM context_persisted_source_index AS idx",
                    "WHERE idx.priority",
                ),
            )
            try:
                ContextRepository(reopened)
                marker = reopened._db.execute(
                    "SELECT event_id FROM context_persisted_source_invalidations "
                    "WHERE event_id = ?",
                    (invalidated_event_id,),
                ).fetchone()
                version = reopened._db.execute(
                    "SELECT version FROM component_schema "
                    "WHERE component = 'bounded_context'"
                ).fetchone()[0]
                self.assertIsNotNone(marker)
                self.assertEqual(int(version), 8)
            finally:
                reopened._connection = original_connection
                reopened.close()

    def test_current_schema_restart_skips_legacy_invalidation_reconciliation(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "host.db"
            programs, _program, _refs = self._setup_controls(database)
            programs.close()

            reopened = ProgramRepository(database)
            original_connection = reopened._connection
            assert original_connection is not None
            reopened._connection = _NoLegacyMigrationConnection(
                original_connection,
                (
                    "FROM context_persisted_source_index AS idx",
                    "LEFT JOIN events ON events.event_id = idx.event_id",
                    "WHERE idx.priority",
                ),
            )
            try:
                ContextRepository(reopened)
                version = reopened._db.execute(
                    "SELECT version FROM component_schema "
                    "WHERE component = 'bounded_context'"
                ).fetchone()[0]
                self.assertEqual(int(version), 8)
            finally:
                reopened._connection = original_connection
                reopened.close()


if __name__ == "__main__":
    unittest.main()
