from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from ai_capital.kernel.context import ContextCompiler, ContextRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ContextPriority
from ai_capital.kernel.errors import IntegrityViolation
from ai_capital.kernel.models import Program


class K8ReviewRound31Tests(unittest.TestCase):
    @staticmethod
    def _event_id(source_ref: str) -> str:
        return source_ref.removeprefix("event:")

    def _setup_host_control(self, directory: str):
        db_path = Path(directory) / "host.db"
        programs = ProgramRepository(db_path)
        program = programs.create(Program("p-1", 0, "restart-safe Host-control invalidation"))
        contexts = ContextRepository(programs)
        source_ref = contexts.persist_source(
            program.program_id,
            priority=ContextPriority.HOST_CONTROL,
            payload={"rule": "mandatory"},
        )
        return db_path, programs, program, contexts, source_ref

    def test_content_plus_event_type_invalidation_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path, programs, program, _contexts, source_ref = self._setup_host_control(directory)
            event_id = self._event_id(source_ref)
            with programs._transaction():
                programs._db.execute(
                    "UPDATE events SET event_json = event_json || ' ', "
                    "event_type = 'context.invalidated' WHERE event_id = ?",
                    (event_id,),
                )
            marker = programs._db.execute(
                "SELECT event_id FROM context_persisted_source_invalidations WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            self.assertIsNotNone(marker)
            programs.close()

            reopened = ProgramRepository(db_path)
            try:
                contexts = ContextRepository(reopened)
                marker = reopened._db.execute(
                    "SELECT event_id FROM context_persisted_source_invalidations WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
                self.assertIsNotNone(marker)
                with self.assertRaises(IntegrityViolation):
                    ContextCompiler(contexts).compile(program.program_id, budget_units=100_000)
            finally:
                reopened.close()

    def test_selector_only_host_control_invalidation_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path, programs, program, _contexts, source_ref = self._setup_host_control(directory)
            event_id = self._event_id(source_ref)
            with programs._transaction():
                programs._db.execute(
                    "UPDATE events SET event_type = 'context.invalidated' WHERE event_id = ?",
                    (event_id,),
                )
            programs.close()

            reopened = ProgramRepository(db_path)
            try:
                contexts = ContextRepository(reopened)
                with self.assertRaises(IntegrityViolation):
                    ContextCompiler(contexts).compile(program.program_id, budget_units=100_000)
            finally:
                reopened.close()

    def test_v6_restart_reconstructs_marker_from_preserved_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path, programs, program, _contexts, source_ref = self._setup_host_control(directory)
            event_id = self._event_id(source_ref)
            with programs._transaction():
                programs._db.execute(
                    "UPDATE events SET event_json = event_json || ' ', "
                    "event_type = 'context.invalidated' WHERE event_id = ?",
                    (event_id,),
                )
                programs._db.execute(
                    "DELETE FROM context_persisted_source_invalidations WHERE event_id = ?",
                    (event_id,),
                )
                programs._db.execute(
                    "UPDATE component_schema SET version = 6 WHERE component = 'bounded_context'"
                )
            projected = programs._db.execute(
                "SELECT event_id FROM context_persisted_source_index WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            self.assertIsNotNone(projected)
            programs.close()

            reopened = ProgramRepository(db_path)
            try:
                contexts = ContextRepository(reopened)
                version = reopened._db.execute(
                    "SELECT version FROM component_schema WHERE component = 'bounded_context'"
                ).fetchone()[0]
                marker = reopened._db.execute(
                    "SELECT event_id FROM context_persisted_source_invalidations WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
                self.assertEqual(int(version), 9)
                self.assertIsNotNone(marker)
                with self.assertRaises(IntegrityViolation):
                    ContextCompiler(contexts).compile(program.program_id, budget_units=100_000)
            finally:
                reopened.close()


if __name__ == "__main__":
    unittest.main()
