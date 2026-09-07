from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from ai_capital.kernel.context import ContextCompiler, ContextRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ContextPriority
from ai_capital.kernel.errors import IntegrityViolation
from ai_capital.kernel.models import Program


class K8ReviewRound30Tests(unittest.TestCase):
    @staticmethod
    def _event_id(source_ref: str) -> str:
        return source_ref.removeprefix("event:")

    def _setup_host_control(self, directory: str):
        programs = ProgramRepository(Path(directory) / "host.db")
        program = programs.create(Program("p-1", 0, "Host-control invalidation evidence"))
        contexts = ContextRepository(programs)
        source_ref = contexts.persist_source(
            program.program_id,
            priority=ContextPriority.HOST_CONTROL,
            payload={"rule": "mandatory"},
        )
        return programs, program, contexts, source_ref

    def test_content_plus_event_type_mutation_preserves_projection_evidence_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            programs, program, contexts, source_ref = self._setup_host_control(directory)
            try:
                event_id = self._event_id(source_ref)
                with programs._transaction():
                    programs._db.execute(
                        "UPDATE events SET event_json = event_json || ' ', "
                        "event_type = 'context.invalidated' WHERE event_id = ?",
                        (event_id,),
                    )
                projected = programs._db.execute(
                    "SELECT priority FROM context_persisted_source_index WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
                self.assertIsNotNone(projected)
                self.assertEqual(projected["priority"], ContextPriority.HOST_CONTROL.value)

                with self.assertRaises(IntegrityViolation):
                    ContextCompiler(contexts).compile(
                        program.program_id,
                        budget_units=100_000,
                    )
            finally:
                programs.close()

    def test_content_plus_priority_mutation_preserves_projection_evidence_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            programs, program, contexts, source_ref = self._setup_host_control(directory)
            try:
                event_id = self._event_id(source_ref)
                with programs._transaction():
                    programs._db.execute(
                        "UPDATE events SET event_json = event_json || ' ', "
                        "context_source_priority = ? WHERE event_id = ?",
                        (ContextPriority.ADVISORY_MEMORY.value, event_id),
                    )
                projected = programs._db.execute(
                    "SELECT priority FROM context_persisted_source_index WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
                self.assertIsNotNone(projected)
                self.assertEqual(projected["priority"], ContextPriority.HOST_CONTROL.value)

                with self.assertRaises(IntegrityViolation):
                    ContextCompiler(contexts).compile(
                        program.program_id,
                        budget_units=100_000,
                    )
            finally:
                programs.close()


if __name__ == "__main__":
    unittest.main()
