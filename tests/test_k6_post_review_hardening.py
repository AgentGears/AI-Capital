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


class RecordingEvidenceRepository(EvidenceRepository):
    def __init__(self, *args, **kwargs):
        self.fsynced_directories: list[Path] = []
        super().__init__(*args, **kwargs)

    def _fsync_directory(self, path: Path) -> None:
        self.fsynced_directories.append(Path(path))


class K6PostReviewHardeningTests(unittest.TestCase):
    @staticmethod
    def _admit(evidence: EvidenceRepository, content: bytes, *, evidence_id: str | None = None):
        return evidence.admit(
            content=content,
            source_class="source_observation",
            observed_at=OBSERVED,
            provenance=("source:fixture", "admission:host"),
            trust_class="observed",
            currentness="current",
            evidence_id=evidence_id,
        )

    def test_existing_artifact_directory_is_refsynced_before_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "kernel.db")
            evidence = RecordingEvidenceRepository(programs)
            try:
                first = self._admit(evidence, b"shared-content", evidence_id="e-1")
                artifact_parent = Path(directory) / "evidence" / first.digest[:2]
                evidence.fsynced_directories.clear()
                self._admit(evidence, b"shared-content", evidence_id="e-2")
                self.assertIn(artifact_parent, evidence.fsynced_directories)
            finally:
                programs.close()

    def test_claim_get_rejects_intermediate_history_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "kernel.db")
            evidence = EvidenceRepository(programs)
            claims = ClaimRepository(programs, evidence)
            try:
                support = self._admit(evidence, b"support")
                contradiction = self._admit(evidence, b"contradiction")
                claim = claims.create("statement")
                claims.support(claim.claim_id, (support.evidence_id,))
                claims.contradict(claim.claim_id, (contradiction.evidence_id,))
                row = programs._db.execute(
                    """
                    SELECT sequence FROM claim_history
                    WHERE claim_id = ? AND event_type = 'claim.supported'
                    """,
                    (claim.claim_id,),
                ).fetchone()
                self.assertIsNotNone(row)
                programs._db.execute(
                    "DELETE FROM claim_history WHERE sequence = ?",
                    (int(row["sequence"]),),
                )
                with self.assertRaises(IntegrityViolation):
                    claims.get(claim.claim_id)
            finally:
                programs.close()

    def test_same_evidence_cannot_support_and_contradict_one_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "kernel.db")
            evidence = EvidenceRepository(programs)
            claims = ClaimRepository(programs, evidence)
            try:
                admitted = self._admit(evidence, b"single-source")
                claim = claims.create("statement")
                claims.support(claim.claim_id, (admitted.evidence_id,))
                with self.assertRaises(EvidenceInvalid):
                    claims.contradict(claim.claim_id, (admitted.evidence_id,))
            finally:
                programs.close()


if __name__ == "__main__":
    unittest.main()
