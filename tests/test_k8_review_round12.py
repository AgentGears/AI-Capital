from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_capital.kernel.context import ContextCompiler, ContextRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ContextPriority
from ai_capital.kernel.models import Program


class K8ReviewRound12Tests(unittest.TestCase):
    def test_recalled_context_preflight_counts_receipt_metadata_before_decode(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "receipt-aware recall preflight"))
                contexts = ContextRepository(programs)
                compiler = ContextCompiler(contexts)
                baseline = compiler.compile(program.program_id, budget_units=100000)
                refs = tuple(
                    contexts.persist_source(
                        program.program_id,
                        priority=ContextPriority.ADVISORY_MEMORY,
                        payload={"blob": (str(index) + "x" * 2048)},
                    )
                    for index in range(96)
                )
                compiled = compiler.compile(
                    program.program_id,
                    budget_units=baseline.used_units,
                    source_refs=refs,
                )
                row = programs._db.execute(
                    """
                    SELECT
                        length(CAST(context_json AS BLOB)) AS context_bytes,
                        length(CAST(receipt_json AS BLOB)) AS receipt_bytes
                    FROM context_receipts
                    WHERE context_receipt_id = ?
                    """,
                    (compiled.receipt.context_receipt_id,),
                ).fetchone()
                self.assertGreater(int(row["receipt_bytes"]), int(row["context_bytes"]))
                budget = 14 + int(row["context_bytes"])
                with patch.object(
                    contexts,
                    "get",
                    side_effect=AssertionError("oversized Context receipt metadata was decoded"),
                ) as get_context:
                    result = contexts.recall(
                        program.program_id,
                        (compiled.receipt.context_receipt_id,),
                        max_items=1,
                        max_units=budget,
                    )
                get_context.assert_not_called()
                self.assertEqual(result.included_refs, ())
                self.assertEqual(
                    result.excluded_refs,
                    (compiled.receipt.context_receipt_id,),
                )
            finally:
                programs.close()

    def test_oversized_ordinary_event_is_excluded_before_event_decode(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "x" * 131072))
                contexts = ContextRepository(programs)
                row = programs._db.execute(
                    """
                    SELECT events.event_id
                    FROM program_projections
                    JOIN events ON events.sequence = program_projections.last_sequence
                    WHERE program_projections.program_id = ?
                    """,
                    (program.program_id,),
                ).fetchone()
                ref = f"event:{row['event_id']}"
                with patch.object(
                    contexts,
                    "_event_by_id",
                    side_effect=AssertionError("oversized ordinary Event was decoded"),
                ) as event_by_id:
                    result = contexts.recall(
                        program.program_id,
                        (ref,),
                        max_items=1,
                        max_units=14,
                    )
                event_by_id.assert_not_called()
                self.assertEqual(result.included_refs, ())
                self.assertEqual(result.excluded_refs, (ref,))
            finally:
                programs.close()

    def test_fitting_ordinary_event_materializes_after_size_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "small Event recall"))
                contexts = ContextRepository(programs)
                row = programs._db.execute(
                    """
                    SELECT events.event_id
                    FROM program_projections
                    JOIN events ON events.sequence = program_projections.last_sequence
                    WHERE program_projections.program_id = ?
                    """,
                    (program.program_id,),
                ).fetchone()
                ref = f"event:{row['event_id']}"
                with patch.object(
                    contexts,
                    "_event_by_id",
                    wraps=contexts._event_by_id,
                ) as event_by_id:
                    result = contexts.recall(
                        program.program_id,
                        (ref,),
                        max_items=1,
                        max_units=100000,
                    )
                self.assertEqual(event_by_id.call_count, 1)
                self.assertEqual(result.included_refs, (ref,))
                self.assertEqual(result.excluded_refs, ())
            finally:
                programs.close()


if __name__ == "__main__":
    unittest.main()
