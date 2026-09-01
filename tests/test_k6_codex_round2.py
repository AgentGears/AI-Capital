from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_capital.kernel.claim_store import ClaimRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.errors import EvidenceInvalid, IntegrityViolation
from ai_capital.kernel.evidence_store import EvidenceRepository


OBSERVED = "2026-08-31T00:00:00Z"


class K6CodexRound2Tests(unittest.TestCase):
    @staticmethod
    def _admit(evidence: EvidenceRepository, content: bytes):
        return evidence.admit(
            content=content,
            source_class="source_observation",
            observed_at=OBSERVED,
            provenance=("source:fixture", "admission:host"),
            trust_class="observed",
            currentness="current",
        )

    def test_scalar_provenance_is_rejected_before_admission(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "kernel.db")
            evidence = EvidenceRepository(programs)
            try:
                with self.assertRaises(EvidenceInvalid):
                    evidence.admit(
                        content=b"source",
                        source_class="source_observation",
                        observed_at=OBSERVED,
                        provenance="source",  # type: ignore[arg-type]
                        trust_class="observed",
                        currentness="current",
                        evidence_id="e-bad-provenance",
                    )
                row = programs._db.execute(
                    "SELECT 1 FROM evidence_records WHERE evidence_id = ?",
                    ("e-bad-provenance",),
                ).fetchone()
                self.assertIsNone(row)
            finally:
                programs.close()

    def test_missing_typed_evidence_relation_is_rejected_against_events(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "kernel.db")
            evidence = EvidenceRepository(programs)
            claims = ClaimRepository(programs, evidence)
            try:
                first = self._admit(evidence, b"first")
                second = self._admit(evidence, b"second")
                claim = claims.create("statement")
                claims.add_reference(claim.claim_id, first.evidence_id)
                claims.support(
                    claim.claim_id,
                    (first.evidence_id, second.evidence_id),
                )
                programs._db.execute(
                    """
                    DELETE FROM claim_evidence_links
                    WHERE claim_id = ? AND evidence_id = ? AND relation = 'support'
                    """,
                    (claim.claim_id, first.evidence_id),
                )
                with self.assertRaises(IntegrityViolation):
                    claims.get(claim.claim_id)
            finally:
                programs.close()


if __name__ == "__main__":
    unittest.main()
