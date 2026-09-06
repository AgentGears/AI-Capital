from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_capital.kernel.context import ContextRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.errors import IntegrityViolation
from ai_capital.kernel.evidence_store import EvidenceRepository
from ai_capital.kernel.models import Program


class K8ReviewRound17Tests(unittest.TestCase):
    def test_empty_recall_does_not_decode_current_program_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-large", 0, "x" * 131072))
                contexts = ContextRepository(programs)
                with patch.object(
                    programs,
                    "get",
                    side_effect=AssertionError("bounded recall decoded current Program"),
                ) as get_program:
                    result = contexts.recall(
                        program.program_id,
                        (),
                        max_items=1,
                        max_units=14,
                    )
                get_program.assert_not_called()
                self.assertEqual(result.included_refs, ())
                self.assertEqual(result.excluded_refs, ())
                self.assertEqual(result.used_units, 14)
            finally:
                programs.close()

    def test_item_limit_does_not_hide_corrupt_evidence_content_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "item-limit Evidence validation"))
                evidence = EvidenceRepository(programs)
                first = evidence.admit(
                    content=b"first exact artifact",
                    source_class="test",
                    observed_at="2026-01-01T00:00:00Z",
                    provenance=("test",),
                    trust_class="test",
                    currentness="current",
                    evidence_id="e-1",
                )
                second = evidence.admit(
                    content=b"second exact artifact",
                    source_class="test",
                    observed_at="2026-01-01T00:00:00Z",
                    provenance=("test",),
                    trust_class="test",
                    currentness="current",
                    evidence_id="e-2",
                )
                programs._db.execute(
                    "UPDATE evidence_artifacts SET content_ref = ? WHERE artifact_digest = ?",
                    ("evidence-artifact:corrupt", second.digest),
                )
                contexts = ContextRepository(programs, evidence)
                refs = (f"evidence:{second.evidence_id}", f"evidence:{first.evidence_id}")
                with patch.object(
                    contexts,
                    "_resolve_recall",
                    side_effect=AssertionError("Evidence payload was materialized"),
                ) as resolve:
                    with self.assertRaises(IntegrityViolation):
                        contexts.recall(
                            program.program_id,
                            refs,
                            max_items=1,
                            max_units=14,
                        )
                resolve.assert_not_called()
            finally:
                programs.close()

    def test_item_limit_does_not_hide_missing_evidence_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "item-limit Evidence artifact validation"))
                evidence = EvidenceRepository(programs)
                first = evidence.admit(
                    content=b"first retained artifact",
                    source_class="test",
                    observed_at="2026-01-01T00:00:00Z",
                    provenance=("test",),
                    trust_class="test",
                    currentness="current",
                    evidence_id="e-1",
                )
                second = evidence.admit(
                    content=b"second missing artifact",
                    source_class="test",
                    observed_at="2026-01-01T00:00:00Z",
                    provenance=("test",),
                    trust_class="test",
                    currentness="current",
                    evidence_id="e-2",
                )
                evidence._artifact_path(second.digest).unlink()
                contexts = ContextRepository(programs, evidence)
                refs = (f"evidence:{first.evidence_id}", f"evidence:{second.evidence_id}")
                with patch.object(
                    contexts,
                    "_resolve_recall",
                    side_effect=AssertionError("Evidence payload was materialized"),
                ) as resolve:
                    with self.assertRaises(IntegrityViolation):
                        contexts.recall(
                            program.program_id,
                            refs,
                            max_items=1,
                            max_units=14,
                        )
                resolve.assert_not_called()
            finally:
                programs.close()


if __name__ == "__main__":
    unittest.main()
