from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_capital.kernel.context import ContextCompiler, ContextRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ContextCompleteness, ContextPriority
from ai_capital.kernel.models import Program


class K8ReviewRound8Tests(unittest.TestCase):
    def test_oversized_persisted_sources_are_excluded_before_event_materialization(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "persisted source preflight"))
                contexts = ContextRepository(programs)
                compiler = ContextCompiler(contexts)
                refs = tuple(
                    contexts.persist_source(
                        program.program_id,
                        priority=ContextPriority.ADVISORY_MEMORY,
                        payload={"ordinal": index, "text": "x" * 131072},
                    )
                    for index in range(3)
                )
                baseline = compiler.compile(program.program_id, budget_units=100_000)

                with patch.object(
                    contexts,
                    "_materialize_persisted_source",
                    side_effect=AssertionError("excluded persisted source was materialized"),
                ) as materialize, patch.object(
                    contexts,
                    "_event_by_id",
                    side_effect=AssertionError("excluded persisted source Event was decoded"),
                ) as event_by_id:
                    compiled = compiler.compile(
                        program.program_id,
                        budget_units=baseline.used_units,
                        source_refs=tuple(reversed(refs)),
                    )

                materialize.assert_not_called()
                event_by_id.assert_not_called()
                self.assertIs(compiled.receipt.completeness, ContextCompleteness.TRUNCATED)
                self.assertEqual(compiled.receipt.excluded_refs, tuple(sorted(refs)))
                self.assertEqual(compiled.used_units, baseline.used_units)
            finally:
                programs.close()

    def test_fitting_persisted_source_materializes_once_after_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "persisted source include"))
                contexts = ContextRepository(programs)
                compiler = ContextCompiler(contexts)
                ref = contexts.persist_source(
                    program.program_id,
                    priority=ContextPriority.RECENT_INTERACTION,
                    payload={"message": "small durable source"},
                )

                with patch.object(
                    contexts,
                    "_materialize_persisted_source",
                    wraps=contexts._materialize_persisted_source,
                ) as materialize:
                    compiled = compiler.compile(
                        program.program_id,
                        budget_units=100_000,
                        source_refs=(ref,),
                    )

                materialize.assert_called_once()
                self.assertIn(ref, compiled.receipt.included_refs)
                self.assertIs(compiled.receipt.completeness, ContextCompleteness.COMPLETE)
            finally:
                programs.close()

    def test_persisted_source_preflight_projection_rebuilds_on_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "host.db"
            programs = ProgramRepository(path)
            program = programs.create(Program("p-1", 0, "persisted source rebuild"))
            contexts = ContextRepository(programs)
            ref = contexts.persist_source(
                program.program_id,
                priority=ContextPriority.ADVISORY_MEMORY,
                payload={"text": "y" * 131072},
            )
            programs._db.execute("DELETE FROM context_persisted_source_index")
            programs.close()

            programs = ProgramRepository(path)
            try:
                contexts = ContextRepository(programs)
                row = programs._db.execute(
                    "SELECT event_id FROM context_persisted_source_index"
                ).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(ref, f"event:{row['event_id']}")
                compiler = ContextCompiler(contexts)
                baseline = compiler.compile(program.program_id, budget_units=100_000)
                with patch.object(
                    contexts,
                    "_materialize_persisted_source",
                    side_effect=AssertionError("rebuilt excluded source was materialized"),
                ) as materialize:
                    compiled = compiler.compile(
                        program.program_id,
                        budget_units=baseline.used_units,
                        source_refs=(ref,),
                    )
                materialize.assert_not_called()
                self.assertEqual(compiled.receipt.excluded_refs, (ref,))
            finally:
                programs.close()


if __name__ == "__main__":
    unittest.main()
