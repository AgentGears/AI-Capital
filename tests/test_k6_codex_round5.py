from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_capital.kernel.claim_store import ClaimRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.errors import EvidenceInvalid, InvalidRequest
from ai_capital.kernel.evidence_store import EvidenceRepository


OBSERVED = "2026-08-31T00:00:00Z"


class K6CodexRound5Tests(unittest.TestCase):
    @staticmethod
    def _admit(evidence: EvidenceRepository, *, evidence_id: str | None):
        return evidence.admit(
            content=b"round-five",
            source_class="source_observation",
            observed_at=OBSERVED,
            provenance=("source:fixture", "admission:host"),
            trust_class="observed",
            currentness="current",
            evidence_id=evidence_id,
        )

    def test_windows_directory_flush_uses_native_path_not_os_open(self):
        path = Path("artifact-parent")
        with (
            patch("ai_capital.kernel.evidence_store.os.name", "nt"),
            patch.object(EvidenceRepository, "_windows_fsync_directory") as native_flush,
            patch("ai_capital.kernel.evidence_store.os.open") as posix_open,
        ):
            EvidenceRepository._fsync_directory(path)
        native_flush.assert_called_once_with(path)
        posix_open.assert_not_called()

    def test_windows_replace_uses_write_through_native_path(self):
        repository = object.__new__(EvidenceRepository)
        source = Path("temporary-artifact")
        destination = Path("artifact-parent") / "artifact"
        with (
            patch("ai_capital.kernel.evidence_store.os.name", "nt"),
            patch.object(EvidenceRepository, "_windows_replace_durable") as native_replace,
            patch.object(EvidenceRepository, "_fsync_directory") as directory_flush,
            patch("ai_capital.kernel.evidence_store.os.replace") as posix_replace,
        ):
            repository._replace_durable(source, destination)
        native_replace.assert_called_once_with(source, destination)
        directory_flush.assert_called_once_with(destination.parent)
        posix_replace.assert_not_called()

    def test_explicit_empty_evidence_identity_is_rejected_without_admission(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "kernel.db")
            evidence = EvidenceRepository(programs)
            try:
                before_events = int(
                    programs._db.execute(
                        "SELECT COUNT(*) FROM events WHERE event_type = 'evidence.admitted'"
                    ).fetchone()[0]
                )
                with self.assertRaises(EvidenceInvalid):
                    self._admit(evidence, evidence_id="")
                after_events = int(
                    programs._db.execute(
                        "SELECT COUNT(*) FROM events WHERE event_type = 'evidence.admitted'"
                    ).fetchone()[0]
                )
                records = int(
                    programs._db.execute("SELECT COUNT(*) FROM evidence_records").fetchone()[0]
                )
                self.assertEqual(after_events, before_events)
                self.assertEqual(records, 0)
            finally:
                programs.close()

    def test_none_evidence_identity_still_generates_one_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "kernel.db")
            evidence = EvidenceRepository(programs)
            try:
                admitted = self._admit(evidence, evidence_id=None)
                self.assertTrue(admitted.evidence_id.strip())
                self.assertEqual(evidence.get(admitted.evidence_id), admitted)
            finally:
                programs.close()

    def test_explicit_empty_claim_identity_is_rejected_without_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "kernel.db")
            evidence = EvidenceRepository(programs)
            claims = ClaimRepository(programs, evidence)
            try:
                with self.assertRaises(InvalidRequest):
                    claims.create("statement", claim_id="")
                projections = int(
                    programs._db.execute("SELECT COUNT(*) FROM claim_projections").fetchone()[0]
                )
                history = int(
                    programs._db.execute("SELECT COUNT(*) FROM claim_history").fetchone()[0]
                )
                self.assertEqual(projections, 0)
                self.assertEqual(history, 0)
            finally:
                programs.close()

    def test_none_claim_identity_still_generates_one_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "kernel.db")
            evidence = EvidenceRepository(programs)
            claims = ClaimRepository(programs, evidence)
            try:
                claim = claims.create("statement", claim_id=None)
                self.assertTrue(claim.claim_id.strip())
                self.assertEqual(claims.get(claim.claim_id), claim)
            finally:
                programs.close()


if __name__ == "__main__":
    unittest.main()
