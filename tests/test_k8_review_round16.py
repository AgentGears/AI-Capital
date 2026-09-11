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


class K8ReviewRound16Tests(unittest.TestCase):
    def _admit_large_metadata_evidence(
        self,
        evidence: EvidenceRepository,
    ):
        return evidence.admit(
            content=b"small exact artifact",
            source_class="test",
            observed_at="2026-01-01T00:00:00Z",
            provenance=("p" * 131072,),
            trust_class="test",
            currentness="current",
        )

    def test_recall_rejects_corrupt_artifact_content_ref_before_budget_truncation(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "content reference recall"))
                evidence = EvidenceRepository(programs)
                item = self._admit_large_metadata_evidence(evidence)
                programs._db.execute(
                    "UPDATE evidence_artifacts SET content_ref = ? WHERE artifact_digest = ?",
                    ("evidence-artifact:corrupt", item.digest),
                )
                contexts = ContextRepository(programs, evidence)
                with patch.object(
                    contexts,
                    "_resolve_recall",
                    side_effect=AssertionError("corrupt Evidence was materialized"),
                ) as resolve:
                    with self.assertRaises(IntegrityViolation):
                        contexts.recall(
                            program.program_id,
                            (f"evidence:{item.evidence_id}",),
                            max_items=1,
                            max_units=14,
                        )
                resolve.assert_not_called()
            finally:
                programs.close()

    def test_current_evidence_rejects_corrupt_artifact_content_ref_before_exclusion(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "content reference current"))
                evidence = EvidenceRepository(programs)
                item = self._admit_large_metadata_evidence(evidence)
                contexts = ContextRepository(programs, evidence)
                baseline = ContextCompiler(contexts).compile(
                    program.program_id,
                    budget_units=100_000,
                )
                programs._db.execute(
                    "UPDATE evidence_artifacts SET content_ref = ? WHERE artifact_digest = ?",
                    ("evidence-artifact:corrupt", item.digest),
                )
                compiler = ContextCompiler(contexts, evidence=evidence)
                with patch.object(
                    evidence,
                    "_row",
                    side_effect=AssertionError("corrupt Evidence record was decoded"),
                ) as full_row:
                    with self.assertRaises(IntegrityViolation):
                        compiler.compile(
                            program.program_id,
                            budget_units=baseline.used_units,
                            evidence_refs=(item.evidence_id,),
                        )
                full_row.assert_not_called()
            finally:
                programs.close()


if __name__ == "__main__":
    unittest.main()
