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


class K8ReviewRound29Tests(unittest.TestCase):
    @staticmethod
    def _event_id(source_ref: str) -> str:
        return source_ref.removeprefix("event:")

    def _setup_source(self, directory: str):
        programs = ProgramRepository(Path(directory) / "host.db")
        program = programs.create(Program("p-1", 0, "projected Event integrity binding"))
        contexts = ContextRepository(programs)
        source_ref = contexts.persist_source(
            program.program_id,
            priority=ContextPriority.ADVISORY_MEMORY,
            payload={"note": "durable"},
        )
        return programs, program, contexts, source_ref

    def test_post_init_event_content_mutation_invalidates_projection_before_compile(self):
        with tempfile.TemporaryDirectory() as directory:
            programs, program, contexts, source_ref = self._setup_source(directory)
            try:
                with programs._transaction():
                    programs._db.execute(
                        "UPDATE events SET event_json = event_json || ' ' WHERE event_id = ?",
                        (self._event_id(source_ref),),
                    )
                projected = programs._db.execute(
                    "SELECT 1 FROM context_persisted_source_index WHERE event_id = ?",
                    (self._event_id(source_ref),),
                ).fetchone()
                self.assertIsNone(projected)

                with patch.object(
                    contexts,
                    "_event_by_id",
                    side_effect=AssertionError("corrupt backing Event was decoded"),
                ) as event_by_id:
                    with self.assertRaises(IntegrityViolation):
                        ContextCompiler(contexts).compile(
                            program.program_id,
                            budget_units=100_000,
                            source_refs=(source_ref,),
                        )
                event_by_id.assert_not_called()
            finally:
                programs.close()

    def test_post_init_event_content_mutation_invalidates_projection_before_recall(self):
        with tempfile.TemporaryDirectory() as directory:
            programs, program, contexts, source_ref = self._setup_source(directory)
            try:
                with programs._transaction():
                    programs._db.execute(
                        "UPDATE events SET event_json = replace(event_json, 'durable', 'tampered') "
                        "WHERE event_id = ?",
                        (self._event_id(source_ref),),
                    )
                with patch.object(
                    contexts,
                    "_event_by_id",
                    side_effect=AssertionError("corrupt backing Event was decoded"),
                ) as event_by_id:
                    with self.assertRaises(IntegrityViolation):
                        contexts.recall(
                            program.program_id,
                            (source_ref,),
                            max_items=1,
                            max_units=2048,
                        )
                event_by_id.assert_not_called()
            finally:
                programs.close()

    def test_v5_store_advances_to_v6_and_installs_content_invalidation_guard(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "Context v6 migration"))
                contexts = ContextRepository(programs)
                source_ref = contexts.persist_source(
                    program.program_id,
                    priority=ContextPriority.RECENT_INTERACTION,
                    payload={"turn": "kept"},
                )
                with programs._transaction():
                    programs._db.execute(
                        "DROP TRIGGER IF EXISTS context_persisted_source_event_content_invalidate"
                    )
                    programs._db.execute(
                        "UPDATE component_schema SET version = 5 WHERE component = 'bounded_context'"
                    )

                ContextRepository(programs)
                version = programs._db.execute(
                    "SELECT version FROM component_schema WHERE component = 'bounded_context'"
                ).fetchone()[0]
                trigger = programs._db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger' AND name = ?",
                    ("context_persisted_source_event_content_invalidate",),
                ).fetchone()
                self.assertEqual(int(version), 6)
                self.assertIsNotNone(trigger)

                with programs._transaction():
                    programs._db.execute(
                        "UPDATE events SET event_json = event_json || ' ' WHERE event_id = ?",
                        (self._event_id(source_ref),),
                    )
                projected = programs._db.execute(
                    "SELECT 1 FROM context_persisted_source_index WHERE event_id = ?",
                    (self._event_id(source_ref),),
                ).fetchone()
                self.assertIsNone(projected)
            finally:
                programs.close()


if __name__ == "__main__":
    unittest.main()
