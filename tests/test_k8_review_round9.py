from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_capital.kernel.context import ContextCompiler, ContextRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ContextPriority
from ai_capital.kernel.errors import IntegrityViolation
from ai_capital.kernel.models import Program


class K8ReviewRound9Tests(unittest.TestCase):
    def test_priority_corruption_is_rejected_before_budget_classification(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "authenticated preflight"))
                contexts = ContextRepository(programs)
                compiler = ContextCompiler(contexts)
                ref = contexts.persist_source(
                    program.program_id,
                    priority=ContextPriority.HOST_CONTROL,
                    payload={"control": "x" * 131072},
                )
                baseline = compiler.compile(program.program_id, budget_units=100_000)
                event_id = ref.removeprefix("event:")
                programs._db.execute(
                    "UPDATE context_persisted_source_index "
                    "SET priority = ? WHERE event_id = ?",
                    (ContextPriority.ADVISORY_MEMORY.value, event_id),
                )

                with patch.object(
                    contexts,
                    "_materialize_persisted_source",
                    side_effect=AssertionError("corrupt source was materialized"),
                ) as materialize, patch.object(
                    contexts,
                    "_event_by_id",
                    side_effect=AssertionError("corrupt source Event was decoded"),
                ) as event_by_id:
                    with self.assertRaises(IntegrityViolation):
                        compiler.compile(
                            program.program_id,
                            budget_units=baseline.used_units,
                            source_refs=(ref,),
                        )

                materialize.assert_not_called()
                event_by_id.assert_not_called()
            finally:
                programs.close()

    def test_projection_digest_corruption_is_rejected_without_event_decode(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "projection digest"))
                contexts = ContextRepository(programs)
                compiler = ContextCompiler(contexts)
                ref = contexts.persist_source(
                    program.program_id,
                    priority=ContextPriority.ADVISORY_MEMORY,
                    payload={"memory": "small"},
                )
                event_id = ref.removeprefix("event:")
                programs._db.execute(
                    "UPDATE context_persisted_source_index "
                    "SET projection_digest = ? WHERE event_id = ?",
                    ("0" * 64, event_id),
                )
                with patch.object(
                    contexts,
                    "_event_by_id",
                    side_effect=AssertionError("corrupt source Event was decoded"),
                ) as event_by_id:
                    with self.assertRaises(IntegrityViolation):
                        compiler.compile(
                            program.program_id,
                            budget_units=100_000,
                            source_refs=(ref,),
                        )
                event_by_id.assert_not_called()
            finally:
                programs.close()

    def test_version_one_projection_migrates_and_rebuilds_authentication(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "host.db"
            programs = ProgramRepository(path)
            program = programs.create(Program("p-1", 0, "projection migration"))
            contexts = ContextRepository(programs)
            ref = contexts.persist_source(
                program.program_id,
                priority=ContextPriority.ADVISORY_MEMORY,
                payload={"memory": "durable"},
            )
            event_id = ref.removeprefix("event:")
            programs._db.execute(
                "DROP INDEX context_persisted_source_program_priority"
            )
            programs._db.execute("DROP TABLE context_persisted_source_index")
            programs._db.execute(
                """
                CREATE TABLE context_persisted_source_index (
                    sequence INTEGER PRIMARY KEY,
                    event_id TEXT NOT NULL UNIQUE,
                    program_id TEXT NOT NULL,
                    program_revision INTEGER NOT NULL,
                    priority TEXT NOT NULL,
                    source_digest TEXT NOT NULL,
                    payload_units INTEGER NOT NULL,
                    event_digest TEXT NOT NULL,
                    FOREIGN KEY(sequence) REFERENCES events(sequence)
                )
                """
            )
            programs._db.execute(
                "UPDATE component_schema SET version = 1 WHERE component = 'bounded_context'"
            )
            programs.close()

            programs = ProgramRepository(path)
            try:
                contexts = ContextRepository(programs)
                row = programs._db.execute(
                    "SELECT projection_digest FROM context_persisted_source_index "
                    "WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
                self.assertIsNotNone(row)
                self.assertTrue(str(row["projection_digest"]).strip())
                version = programs._db.execute(
                    "SELECT version FROM component_schema WHERE component = 'bounded_context'"
                ).fetchone()
                self.assertEqual(int(version["version"]), 2)
                compiler = ContextCompiler(contexts)
                compiled = compiler.compile(
                    program.program_id,
                    budget_units=100_000,
                    source_refs=(ref,),
                )
                self.assertIn(ref, compiled.receipt.included_refs)
            finally:
                programs.close()


if __name__ == "__main__":
    unittest.main()
