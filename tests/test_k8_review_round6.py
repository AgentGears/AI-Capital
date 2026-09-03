
from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_capital.kernel.context import ContextRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ContextCompleteness, ContextPriority
from ai_capital.kernel.models import Program


class K8ReviewRound6Tests(unittest.TestCase):
    def test_skipped_event_validation_does_not_decode_event_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(
                    Program("p-1", 0, "bounded Event validation")
                )
                contexts = ContextRepository(programs)
                refs = tuple(
                    contexts.persist_source(
                        program.program_id,
                        priority=ContextPriority.ADVISORY_MEMORY,
                        payload={"blob": (f"payload-{index}-" + "x" * 131072)},
                    )
                    for index in range(3)
                )

                with patch.object(
                    contexts,
                    "_event_by_id",
                    wraps=contexts._event_by_id,
                ) as event_by_id, patch.object(
                    contexts,
                    "_decode_event_row",
                    wraps=contexts._decode_event_row,
                ) as decode_event_row:
                    result = contexts.recall(
                        program.program_id,
                        refs,
                        max_items=1,
                        max_units=500_000,
                    )

                self.assertEqual(event_by_id.call_count, 1)
                self.assertEqual(decode_event_row.call_count, 1)
                self.assertEqual(len(result.included_refs), 1)
                self.assertEqual(len(result.excluded_refs), 2)
                self.assertIs(
                    result.completeness,
                    ContextCompleteness.TRUNCATED,
                )
            finally:
                programs.close()

    def test_event_recall_index_backfills_preexisting_program_event(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(
                    Program("p-1", 0, "Event index backfill")
                )
                contexts = ContextRepository(programs)
                current = contexts.current_program_source(program.program_id)
                result = contexts.recall(
                    program.program_id,
                    (current.source_ref,),
                    max_items=1,
                    max_units=100_000,
                )
                self.assertEqual(
                    result.included_refs,
                    (current.source_ref,),
                )
            finally:
                programs.close()


if __name__ == "__main__":
    unittest.main()
