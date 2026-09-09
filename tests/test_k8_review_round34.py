from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_capital.kernel.context import ContextCompiler, ContextRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ContextPriority
from ai_capital.kernel.errors import IntegrityViolation
from ai_capital.kernel.evidence_store import EvidenceRepository
from ai_capital.kernel.models import Program


class _CursorGuard:
    def __init__(self, cursor, sql: str, forbidden: tuple[str, ...]):
        self._cursor = cursor
        self._sql = sql
        self._forbidden = forbidden

    def __iter__(self):
        return iter(self._cursor)

    def fetchall(self):
        if all(fragment in self._sql for fragment in self._forbidden):
            raise AssertionError("unbounded startup projection materialization")
        return self._cursor.fetchall()

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _ConnectionGuard:
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


class K8ReviewRound34Tests(unittest.TestCase):
    def _program_event_ref(self, programs: ProgramRepository, program_id: str) -> str:
        row = programs._db.execute(
            """
            SELECT events.event_id
            FROM program_projections
            JOIN events ON events.sequence = program_projections.last_sequence
            WHERE program_projections.program_id = ?
            """,
            (program_id,),
        ).fetchone()
        return f"event:{row['event_id']}"

    def _admit(self, evidence: EvidenceRepository):
        return evidence.admit(
            content=b"round-34-evidence",
            source_class="test",
            observed_at="2026-01-01T00:00:00Z",
            provenance=("round-34",),
            trust_class="test",
            currentness="current",
        )

    def test_mutated_ordinary_event_fails_before_recall_truncation_and_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "host.db"
            programs = ProgramRepository(database)
            program = programs.create(Program("p-1", 0, "ordinary Event recall authentication"))
            contexts = ContextRepository(programs)
            ref = self._program_event_ref(programs, program.program_id)
            event_id = ref.removeprefix("event:")
            programs._db.execute(
                "UPDATE events SET event_json = event_json || ' ' WHERE event_id = ?",
                (event_id,),
            )
            marker = programs._db.execute(
                "SELECT context_recall_invalidated FROM events WHERE event_id = ?",
                (event_id,),
            ).fetchone()[0]
            self.assertEqual(int(marker), 1)
            with patch.object(
                contexts,
                "_event_by_id",
                side_effect=AssertionError("corrupt Event reached materialization"),
            ) as event_by_id:
                with self.assertRaisesRegex(IntegrityViolation, "Event recall source was invalidated"):
                    contexts.recall(program.program_id, (ref,), max_items=1, max_units=14)
            event_by_id.assert_not_called()
            programs.close()

            reopened = ProgramRepository(database)
            try:
                contexts = ContextRepository(reopened)
                with self.assertRaisesRegex(IntegrityViolation, "Event recall source was invalidated"):
                    contexts.recall(program.program_id, (ref,), max_items=1, max_units=14)
                version = reopened._db.execute(
                    "SELECT version FROM component_schema WHERE component = 'bounded_context'"
                ).fetchone()[0]
                self.assertEqual(int(version), 10)
            finally:
                reopened.close()

    def test_historical_evidence_mutation_fails_before_recall_exclusion(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "historical Evidence authentication"))
                evidence = EvidenceRepository(programs)
                item = self._admit(evidence)
                contexts = ContextRepository(programs, evidence)
                admitted_event_id = str(evidence._row(item.evidence_id)["admitted_event_id"])
                programs._db.execute(
                    "UPDATE events SET event_json = event_json || ' ' WHERE event_id = ?",
                    (admitted_event_id,),
                )
                ref = f"evidence:{item.evidence_id}"
                with patch.object(
                    contexts,
                    "_resolve_recall",
                    side_effect=AssertionError("corrupt Evidence reached recall materialization"),
                ) as resolve:
                    with self.assertRaisesRegex(IntegrityViolation, "Evidence recall Event binding mismatch"):
                        contexts.recall(program.program_id, (ref,), max_items=1, max_units=14)
                resolve.assert_not_called()
            finally:
                programs.close()

    def test_context_receipt_mutation_invalidates_projection_before_recall_truncation(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "Context receipt projection authentication"))
                contexts = ContextRepository(programs)
                compiled = ContextCompiler(contexts).compile(program.program_id, budget_units=100000)
                receipt_id = compiled.receipt.context_receipt_id
                programs._db.execute(
                    "UPDATE context_receipts SET context_json = context_json || ' ' "
                    "WHERE context_receipt_id = ?",
                    (receipt_id,),
                )
                self.assertIsNone(
                    programs._db.execute(
                        "SELECT 1 FROM context_receipt_event_index WHERE context_receipt_id = ?",
                        (receipt_id,),
                    ).fetchone()
                )
                with patch.object(
                    contexts,
                    "get",
                    side_effect=AssertionError("corrupt Context receipt was decoded"),
                ) as get_context:
                    with self.assertRaisesRegex(IntegrityViolation, "semantic Event binding"):
                        contexts.recall(program.program_id, (receipt_id,), max_items=1, max_units=14)
                get_context.assert_not_called()
            finally:
                programs.close()

    def test_persisted_source_projection_rebuild_does_not_fetchall_event_bodies(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "host.db"
            programs = ProgramRepository(database)
            program = programs.create(Program("p-1", 0, "stream persisted-source rebuild"))
            contexts = ContextRepository(programs)
            for index in range(8):
                contexts.persist_source(
                    program.program_id,
                    priority=ContextPriority.ADVISORY_MEMORY,
                    payload={"index": index, "blob": "x" * 4096},
                )
            programs.close()

            reopened = ProgramRepository(database)
            original_connection = reopened._connection
            assert original_connection is not None
            reopened._connection = _ConnectionGuard(
                original_connection,
                ("FROM events", "context.source_persisted", "event_json"),
            )
            try:
                ContextRepository(reopened)
            finally:
                reopened._connection = original_connection
                reopened.close()

    def test_receipt_rebuild_and_audit_do_not_fetchall_compiled_history(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "host.db"
            programs = ProgramRepository(database)
            program = programs.create(Program("p-1", 0, "stream receipt reconciliation"))
            contexts = ContextRepository(programs)
            compiler = ContextCompiler(contexts)
            for _ in range(8):
                compiler.compile(program.program_id, budget_units=100000)
            programs.close()

            reopened = ProgramRepository(database)
            original_connection = reopened._connection
            assert original_connection is not None
            reopened._connection = _ConnectionGuard(
                original_connection,
                ("FROM events", "context.compiled", "event_json"),
            )
            try:
                ContextRepository(reopened)
            finally:
                reopened._connection = original_connection
                reopened.close()

    def test_legacy_evidence_metadata_migration_does_not_fetchall_records(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "host.db"
            programs = ProgramRepository(database)
            evidence = EvidenceRepository(programs)
            for _ in range(8):
                self._admit(evidence)
            programs._db.execute(
                "UPDATE component_schema SET version = 2 WHERE component = 'evidence_store'"
            )
            programs.close()

            reopened = ProgramRepository(database)
            original_connection = reopened._connection
            assert original_connection is not None
            reopened._connection = _ConnectionGuard(
                original_connection,
                ("FROM evidence_records", "evidence_json", "admission_json"),
            )
            try:
                EvidenceRepository(reopened)
            finally:
                reopened._connection = original_connection
                reopened.close()


if __name__ == "__main__":
    unittest.main()
