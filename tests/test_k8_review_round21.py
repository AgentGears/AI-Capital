from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from ai_capital.kernel.context import ContextCompiler, ContextRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ContextPriority
from ai_capital.kernel.models import Program


class K8ReviewRound21Tests(unittest.TestCase):
    def test_host_control_coverage_query_does_not_parse_persisted_event_json(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "scalar Host-control authority"))
                contexts = ContextRepository(programs)
                contexts.persist_source(program.program_id, priority=ContextPriority.HOST_CONTROL, payload={"control":"required"})
                contexts.persist_source(program.program_id, priority=ContextPriority.ADVISORY_MEMORY, payload={"memory":"x" * 131072})
                statements: list[str] = []
                programs._db.set_trace_callback(statements.append)
                try:
                    compiled = ContextCompiler(contexts).compile(program.program_id, budget_units=200000)
                finally:
                    programs._db.set_trace_callback(None)
                self.assertTrue(compiled.receipt.included_refs)
                self.assertNotIn("json_extract", "\n".join(statements).lower())
            finally:
                programs.close()

    def test_scalar_host_control_metadata_survives_restart_backfill(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "host.db"
            programs = ProgramRepository(path)
            program = programs.create(Program("p-1", 0, "restart scalar authority"))
            contexts = ContextRepository(programs)
            ref = contexts.persist_source(program.program_id, priority=ContextPriority.HOST_CONTROL, payload={"control":"required"})
            programs._db.execute(
                "UPDATE events SET context_source_program_id = NULL, context_source_program_revision = NULL, "
                "context_source_priority = NULL, context_source_metadata_digest = NULL WHERE event_id = ?",
                (ref.removeprefix("event:"),),
            )
            programs.close()
            programs = ProgramRepository(path)
            try:
                contexts = ContextRepository(programs)
                compiled = ContextCompiler(contexts).compile(program.program_id, budget_units=100000)
                self.assertIn(ref, compiled.receipt.included_refs)
            finally:
                programs.close()


if __name__ == "__main__":
    unittest.main()
