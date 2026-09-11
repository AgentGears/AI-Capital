from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_capital.kernel.context import ContextCompiler, ContextRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ContextPriority
from ai_capital.kernel.evidence_store import EvidenceRepository
from ai_capital.kernel.models import Program


class K8ReviewRound24Tests(unittest.TestCase):
    def test_long_program_id_persisted_source_compiles_and_recalls(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p" * 8192, 0, "long persisted identity"))
                contexts = ContextRepository(programs)
                ref = contexts.persist_source(
                    program.program_id,
                    priority=ContextPriority.ADVISORY_MEMORY,
                    payload={"memory": "small"},
                )
                compiled = ContextCompiler(contexts).compile(
                    program.program_id, budget_units=100000, source_refs=(ref,)
                )
                self.assertIn(ref, compiled.receipt.included_refs)
                recalled = contexts.recall(
                    program.program_id, (ref,), max_items=1, max_units=100000
                )
                self.assertEqual(recalled.included_refs, (ref,))
            finally:
                programs.close()

    def test_long_program_id_compiled_context_remains_recallable(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p" * 8192, 0, "long compiled identity"))
                contexts = ContextRepository(programs)
                compiled = ContextCompiler(contexts).compile(program.program_id, budget_units=100000)
                recalled = contexts.recall(
                    program.program_id,
                    (compiled.receipt.context_receipt_id,),
                    max_items=1,
                    max_units=100000,
                )
                self.assertEqual(recalled.included_refs, (compiled.receipt.context_receipt_id,))
            finally:
                programs.close()

    def test_long_evidence_id_fits_current_and_historical_storage_envelopes(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "long Evidence identity"))
                evidence = EvidenceRepository(programs)
                item = evidence.admit(
                    evidence_id="e" * 8192,
                    content=b"small",
                    source_class="test",
                    observed_at="2026-01-01T00:00:00Z",
                    provenance=("test",),
                    trust_class="test",
                    currentness="current",
                )
                contexts = ContextRepository(programs, evidence)
                compiler = ContextCompiler(contexts, evidence=evidence)
                compiled = compiler.compile(
                    program.program_id, budget_units=200000, evidence_refs=(item.evidence_id,)
                )
                ref = f"evidence:{item.evidence_id}"
                self.assertIn(ref, compiled.receipt.included_refs)
                recalled = contexts.recall(
                    program.program_id, (ref,), max_items=1, max_units=200000
                )
                self.assertEqual(recalled.included_refs, (ref,))
            finally:
                programs.close()

    def test_failed_current_evidence_candidates_are_rejected_before_record_decode(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "bounded Evidence candidates"))
                evidence = EvidenceRepository(programs)
                items = tuple(
                    evidence.admit(
                        content=f"small-{index}".encode(),
                        source_class="test",
                        observed_at="2026-01-01T00:00:00Z",
                        provenance=("test",),
                        trust_class="test",
                        currentness="current",
                    )
                    for index in range(3)
                )
                contexts = ContextRepository(programs, evidence)
                compiler = ContextCompiler(contexts, evidence=evidence)
                baseline = compiler.compile(program.program_id, budget_units=100000)
                metadata = evidence._metadata_row(items[0].evidence_id)
                encoded_length = 4 * ((len(b"small-0") + 2) // 3)
                coarse_budget = baseline.used_units + int(metadata["evidence_json_bytes"]) + encoded_length
                with patch.object(
                    evidence,
                    "_row",
                    side_effect=AssertionError("over-budget Evidence record was decoded"),
                ) as row:
                    compiled = compiler.compile(
                        program.program_id,
                        budget_units=coarse_budget,
                        evidence_refs=tuple(item.evidence_id for item in items),
                    )
                row.assert_not_called()
                for item in items:
                    self.assertIn(f"evidence:{item.evidence_id}", compiled.receipt.excluded_refs)
            finally:
                programs.close()


if __name__ == "__main__":
    unittest.main()
