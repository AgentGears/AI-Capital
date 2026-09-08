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


class K8ReviewRound19Tests(unittest.TestCase):
    def _evidence(self, programs: ProgramRepository):
        evidence = EvidenceRepository(programs)
        item = evidence.admit(
            content=b"small",
            source_class="test",
            observed_at="2026-01-01T00:00:00Z",
            provenance=("test",),
            trust_class="test",
            currentness="current",
        )
        metadata = evidence._metadata_row(item.evidence_id)
        return evidence, item, str(metadata["admitted_event_id"])

    def test_oversized_evidence_event_is_excluded_before_historical_materialization(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "bounded Evidence Event storage"))
                evidence, item, event_id = self._evidence(programs)
                programs._db.execute(
                    "UPDATE events SET event_json = event_json || ? WHERE event_id = ?",
                    (" " * 131072, event_id),
                )
                contexts = ContextRepository(programs, evidence)
                with patch.object(contexts, "_resolve_recall", side_effect=AssertionError("oversized Evidence Event reached materialization")) as resolve:
                    result = contexts.recall(
                        program.program_id,
                        (f"evidence:{item.evidence_id}",),
                        max_items=1,
                        max_units=100000,
                    )
                resolve.assert_not_called()
                self.assertEqual(result.included_refs, ())
                self.assertEqual(result.excluded_refs, (f"evidence:{item.evidence_id}",))
            finally:
                programs.close()

    def test_oversized_evidence_event_is_excluded_before_current_record_decode(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "bounded current Evidence Event storage"))
                evidence, item, event_id = self._evidence(programs)
                programs._db.execute(
                    "UPDATE events SET event_json = event_json || ? WHERE event_id = ?",
                    (" " * 131072, event_id),
                )
                contexts = ContextRepository(programs, evidence)
                compiler = ContextCompiler(contexts, evidence=evidence)
                with patch.object(evidence, "_row", side_effect=AssertionError("oversized Evidence Event allowed record decode")) as full_row:
                    with self.assertRaisesRegex(
                        IntegrityViolation, "Evidence preflight Event binding mismatch"
                    ):
                        compiler.compile(
                            program.program_id,
                            budget_units=100000,
                            evidence_refs=(item.evidence_id,),
                        )
                full_row.assert_not_called()
            finally:
                programs.close()

    def test_oversized_compiled_context_event_is_excluded_before_receipt_decode(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "bounded compiled Context Event storage"))
                contexts = ContextRepository(programs)
                compiled = ContextCompiler(contexts).compile(program.program_id, budget_units=100000)
                row = programs._db.execute(
                    "SELECT compiled_event_id FROM context_receipts WHERE context_receipt_id = ?",
                    (compiled.receipt.context_receipt_id,),
                ).fetchone()
                programs._db.execute(
                    "UPDATE events SET event_json = event_json || ? WHERE event_id = ?",
                    (" " * 131072, str(row["compiled_event_id"])),
                )
                with patch.object(contexts, "get", side_effect=AssertionError("oversized compiled Context Event was decoded")) as get_context:
                    result = contexts.recall(
                        program.program_id,
                        (compiled.receipt.context_receipt_id,),
                        max_items=1,
                        max_units=100000,
                    )
                get_context.assert_not_called()
                self.assertEqual(result.included_refs, ())
                self.assertEqual(result.excluded_refs, (compiled.receipt.context_receipt_id,))
            finally:
                programs.close()


if __name__ == "__main__":
    unittest.main()
