from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_capital.kernel.context import ContextCompiler, ContextRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.evidence_store import EvidenceRepository
from ai_capital.kernel.models import Program


class K8ReviewRound13Tests(unittest.TestCase):
    def test_evidence_address_validation_does_not_load_full_records(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "metadata-light Evidence validation"))
                evidence = EvidenceRepository(programs)
                refs = []
                for index in range(3):
                    item = evidence.admit(
                        content=(b"x" * 4096) + bytes([index]),
                        source_class="test",
                        observed_at="2026-01-01T00:00:00Z",
                        provenance=("p" * 131072,),
                        trust_class="test",
                        currentness="current",
                    )
                    refs.append(f"evidence:{item.evidence_id}")
                contexts = ContextRepository(programs, evidence)
                with patch.object(
                    evidence,
                    "_row",
                    side_effect=AssertionError("Evidence validation loaded full durable records"),
                ) as full_row:
                    result = contexts.recall(
                        program.program_id,
                        tuple(refs),
                        max_items=1,
                        max_units=14,
                    )
                full_row.assert_not_called()
                self.assertEqual(result.included_refs, ())
                self.assertEqual(len(result.excluded_refs), 3)
            finally:
                programs.close()

    def test_oversized_current_evidence_metadata_is_excluded_before_decode(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "current Evidence metadata preflight"))
                evidence = EvidenceRepository(programs)
                item = evidence.admit(
                    content=b"small",
                    source_class="test",
                    observed_at="2026-01-01T00:00:00Z",
                    provenance=("p" * 131072,),
                    trust_class="test",
                    currentness="current",
                )
                contexts = ContextRepository(programs, evidence)
                compiler = ContextCompiler(contexts, evidence=evidence)
                baseline = compiler.compile(program.program_id, budget_units=100000)
                with patch.object(
                    evidence,
                    "_row",
                    side_effect=AssertionError("oversized current Evidence metadata was decoded"),
                ) as full_row:
                    compiled = compiler.compile(
                        program.program_id,
                        budget_units=baseline.used_units,
                        evidence_refs=(item.evidence_id,),
                    )
                full_row.assert_not_called()
                self.assertEqual(compiled.receipt.included_refs, baseline.receipt.included_refs)
                self.assertEqual(compiled.receipt.excluded_refs, (f"evidence:{item.evidence_id}",))
            finally:
                programs.close()

    def test_historical_evidence_metadata_is_preflighted_before_materialization(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "historical Evidence metadata bound"))
                evidence = EvidenceRepository(programs)
                item = evidence.admit(
                    content=b"small",
                    source_class="test",
                    observed_at="2026-01-01T00:00:00Z",
                    provenance=("p" * 131072,),
                    trust_class="test",
                    currentness="current",
                )
                contexts = ContextRepository(programs, evidence)
                ref = f"evidence:{item.evidence_id}"
                with patch.object(
                    evidence,
                    "_row",
                    side_effect=AssertionError("oversized historical Evidence metadata was decoded"),
                ) as full_row:
                    result = contexts.recall(
                        program.program_id,
                        (ref,),
                        max_items=1,
                        max_units=1024,
                    )
                full_row.assert_not_called()
                self.assertEqual(result.included_refs, ())
                self.assertEqual(result.excluded_refs, (ref,))
            finally:
                programs.close()

    def test_current_evidence_admission_storage_is_bounded_before_decode(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "current Evidence admission bound"))
                evidence = EvidenceRepository(programs)
                item = evidence.admit(
                    content=b"small",
                    source_class="test",
                    observed_at="2026-01-01T00:00:00Z",
                    provenance=("test",),
                    trust_class="test",
                    currentness="current",
                )
                programs._db.execute(
                    "UPDATE evidence_records SET admission_json = admission_json || ? WHERE evidence_id = ?",
                    (" " * 131072, item.evidence_id),
                )
                contexts = ContextRepository(programs, evidence)
                compiler = ContextCompiler(contexts, evidence=evidence)
                baseline = compiler.compile(program.program_id, budget_units=100000)
                with patch.object(
                    evidence,
                    "_row",
                    side_effect=AssertionError("oversized Evidence admission storage was decoded"),
                ) as full_row:
                    compiled = compiler.compile(
                        program.program_id,
                        budget_units=baseline.used_units + 1024,
                        evidence_refs=(item.evidence_id,),
                    )
                full_row.assert_not_called()
                self.assertEqual(compiled.receipt.excluded_refs, (f"evidence:{item.evidence_id}",))
            finally:
                programs.close()


if __name__ == "__main__":
    unittest.main()
