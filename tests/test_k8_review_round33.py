from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_capital.kernel.context import ContextCompiler, ContextRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.errors import IntegrityViolation
from ai_capital.kernel.evidence_store import EvidenceRepository
from ai_capital.kernel.models import Program


class _CursorGuard:
    def __init__(self, cursor, sql: str):
        self._cursor = cursor
        self._sql = sql

    def __iter__(self):
        return iter(self._cursor)

    def __getattr__(self, name):
        return getattr(self._cursor, name)

    def fetchall(self):
        if "FROM events" in self._sql and "event_json" in self._sql:
            raise AssertionError("Evidence Event-index rebuild materialized all Event rows")
        return self._cursor.fetchall()


class _ConnectionGuard:
    def __init__(self, connection):
        self._connection = connection

    def execute(self, sql, parameters=()):
        return _CursorGuard(self._connection.execute(sql, parameters), sql)

    def __getattr__(self, name):
        return getattr(self._connection, name)


class K8ReviewRound33Tests(unittest.TestCase):
    def _admit_current(self, evidence: EvidenceRepository):
        return evidence.admit(
            content=b"round-33-evidence",
            source_class="test",
            observed_at="2026-01-01T00:00:00Z",
            provenance=("round-33",),
            trust_class="test",
            currentness="current",
        )

    def test_mutated_inflated_event_fails_before_storage_exclusion(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "authenticate before storage exclusion"))
                evidence = EvidenceRepository(programs)
                item = self._admit_current(evidence)
                contexts = ContextRepository(programs, evidence)
                compiler = ContextCompiler(contexts, evidence=evidence)
                baseline = compiler.compile(program.program_id, budget_units=100000)
                admitted_event_id = str(evidence._row(item.evidence_id)["admitted_event_id"])
                programs._db.execute(
                    "UPDATE events SET event_json = event_json || ? WHERE event_id = ?",
                    (" " * 100000, admitted_event_id),
                )
                self.assertIsNone(
                    programs._db.execute(
                        "SELECT 1 FROM evidence_event_index WHERE event_id = ?",
                        (admitted_event_id,),
                    ).fetchone()
                )
                with patch.object(
                    evidence,
                    "_row",
                    side_effect=AssertionError("corrupt Evidence reached full record decode"),
                ) as full_row:
                    with self.assertRaisesRegex(
                        IntegrityViolation, "Evidence preflight Event binding mismatch"
                    ):
                        compiler.compile(
                            program.program_id,
                            budget_units=baseline.used_units,
                            evidence_refs=(item.evidence_id,),
                        )
                full_row.assert_not_called()
            finally:
                programs.close()

    def test_v2_migration_reauthenticates_existing_evidence_events(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "host.db"
            programs = ProgramRepository(database)
            evidence = EvidenceRepository(programs)
            item = self._admit_current(evidence)
            admitted_event_id = str(evidence._row(item.evidence_id)["admitted_event_id"])
            programs._db.execute(
                "DROP TRIGGER IF EXISTS evidence_admission_event_integrity_invalidate"
            )
            programs._db.execute(
                "UPDATE component_schema SET version = 2 WHERE component = 'evidence_store'"
            )
            programs._db.execute(
                "UPDATE events SET event_digest = ? WHERE event_id = ?",
                ("0" * 64, admitted_event_id),
            )
            programs.close()

            reopened = ProgramRepository(database)
            try:
                with self.assertRaisesRegex(IntegrityViolation, "Evidence Event integrity mismatch"):
                    EvidenceRepository(reopened)
            finally:
                reopened.close()

    def test_v3_event_index_rebuild_streams_only_admission_events(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "host.db"
            programs = ProgramRepository(database)
            for index in range(5):
                programs.create(
                    Program(
                        f"noise-{index}",
                        0,
                        "unrelated durable history " + ("x" * 20000),
                    )
                )
            evidence = EvidenceRepository(programs)
            self._admit_current(evidence)
            programs._db.execute(
                "DROP TRIGGER IF EXISTS evidence_admission_event_integrity_invalidate"
            )
            programs._db.execute(
                "UPDATE component_schema SET version = 3 WHERE component = 'evidence_store'"
            )
            programs.close()

            reopened = ProgramRepository(database)
            original_connection = reopened._connection
            assert original_connection is not None
            reopened._connection = _ConnectionGuard(original_connection)
            decoded_types: list[str] = []
            original_decode = EvidenceRepository._decode_event_row

            def guarded_decode(repository, row):
                event_type = str(row["event_type"])
                decoded_types.append(event_type)
                if event_type != "evidence.admitted":
                    raise AssertionError("unrelated Event body reached Evidence migration decode")
                return original_decode(repository, row)

            try:
                with patch.object(EvidenceRepository, "_decode_event_row", new=guarded_decode):
                    migrated = EvidenceRepository(reopened)
                self.assertEqual(decoded_types, ["evidence.admitted"])
                version = original_connection.execute(
                    "SELECT version FROM component_schema WHERE component = 'evidence_store'"
                ).fetchone()[0]
                self.assertEqual(int(version), 4)
                self.assertIsNotNone(migrated)
            finally:
                reopened._connection = original_connection
                reopened.close()


if __name__ == "__main__":
    unittest.main()
