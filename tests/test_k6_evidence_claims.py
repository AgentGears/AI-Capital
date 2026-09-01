from __future__ import annotations

from dataclasses import fields, replace
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_capital.kernel.claim_store import (
    ClaimEvidenceLink,
    ClaimEvidenceRelation,
    ClaimRepository,
    DispositionInputs,
)
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ClaimStatus
from ai_capital.kernel.errors import (
    EvidenceInvalid,
    EvidenceMissing,
    IntegrityViolation,
    PersistenceConflict,
)
from ai_capital.kernel.evidence_store import EvidenceReference, EvidenceRepository
from ai_capital.kernel.models import ClaimProposal
from ai_capital.kernel.schema_codec import record_to_json
from ai_capital.kernel.serialization import canonical_digest


OBSERVED = "2026-08-31T00:00:00Z"


class RecordingEvidenceRepository(EvidenceRepository):
    def __init__(self, *args, **kwargs):
        self.fsynced_directories: list[Path] = []
        super().__init__(*args, **kwargs)

    def _fsync_directory(self, path: Path) -> None:
        self.fsynced_directories.append(Path(path))


class K6EvidenceClaimTests(unittest.TestCase):
    def _stores(self, directory: str):
        programs = ProgramRepository(Path(directory) / "kernel.db")
        evidence = EvidenceRepository(programs)
        claims = ClaimRepository(programs, evidence)
        return programs, evidence, claims

    @staticmethod
    def _admit(
        evidence: EvidenceRepository,
        content: bytes,
        *,
        currentness: str = "current",
    ):
        return evidence.admit(
            content=content,
            source_class="source_observation",
            observed_at=OBSERVED,
            provenance=("source:fixture", "admission:host"),
            trust_class="observed",
            currentness=currentness,
        )

    def test_changing_source_bytes_changes_evidence_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            programs, evidence, _ = self._stores(directory)
            try:
                first = self._admit(evidence, b"alpha")
                second = self._admit(evidence, b"beta")
                self.assertEqual(first.digest, hashlib.sha256(b"alpha").hexdigest())
                self.assertNotEqual(first.digest, second.digest)
            finally:
                programs.close()

    def test_evidence_admission_is_explicit_and_requires_exact_bytes_and_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            programs, evidence, _ = self._stores(directory)
            try:
                with self.assertRaises(EvidenceInvalid):
                    evidence.admit(
                        content="model summary",
                        source_class="model_text",
                        observed_at=OBSERVED,
                        provenance=("model:attempt",),
                        trust_class="advisory",
                        currentness="current",
                    )
                with self.assertRaises(EvidenceInvalid):
                    evidence.admit(
                        content=b"bytes",
                        source_class="source_observation",
                        observed_at=OBSERVED,
                        provenance=(),
                        trust_class="observed",
                        currentness="current",
                    )
            finally:
                programs.close()

    def test_evidence_bytes_use_filesystem_artifact_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            programs, evidence, _ = self._stores(directory)
            try:
                admitted = self._admit(evidence, b"filesystem-evidence")
                columns = {
                    row["name"]
                    for row in programs._db.execute(
                        "PRAGMA table_info(evidence_artifacts)"
                    ).fetchall()
                }
                self.assertNotIn("content", columns)
                artifact_path = (
                    Path(directory)
                    / "evidence"
                    / admitted.digest[:2]
                    / admitted.digest[2:]
                )
                self.assertEqual(artifact_path.read_bytes(), b"filesystem-evidence")
                self.assertEqual(evidence.artifact(admitted.evidence_id), b"filesystem-evidence")
            finally:
                programs.close()

    def test_artifact_directory_is_fsynced_before_admission_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "kernel.db")
            evidence = RecordingEvidenceRepository(programs)
            try:
                evidence.fsynced_directories.clear()
                admitted = self._admit(evidence, b"durable-directory-entry")
                artifact_parent = (
                    Path(directory)
                    / "evidence"
                    / admitted.digest[:2]
                )
                self.assertIn(artifact_parent, evidence.fsynced_directories)
            finally:
                programs.close()

    def test_evidence_identity_cannot_be_reused_when_projection_is_lost(self):
        with tempfile.TemporaryDirectory() as directory:
            programs, evidence, _ = self._stores(directory)
            try:
                evidence.admit(
                    content=b"first",
                    source_class="source_observation",
                    observed_at=OBSERVED,
                    provenance=("source:fixture", "admission:host"),
                    trust_class="observed",
                    currentness="current",
                    evidence_id="e-fixed",
                )
                programs._db.execute(
                    "DELETE FROM evidence_records WHERE evidence_id = ?",
                    ("e-fixed",),
                )
                with self.assertRaises(PersistenceConflict):
                    evidence.admit(
                        content=b"second",
                        source_class="source_observation",
                        observed_at=OBSERVED,
                        provenance=("source:fixture", "admission:host"),
                        trust_class="observed",
                        currentness="current",
                        evidence_id="e-fixed",
                    )
            finally:
                programs.close()

    def test_model_claim_proposal_does_not_admit_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            programs, evidence, claims = self._stores(directory)
            try:
                proposal = ClaimProposal("proposed statement", ("model-evidence-id",))
                claim = claims.create(proposal.statement)
                self.assertEqual(claim.evidence_refs, ())
                self.assertIs(claim.status, ClaimStatus.PROPOSED)
                with self.assertRaises(EvidenceMissing):
                    evidence.get("model-evidence-id")
            finally:
                programs.close()

    def test_supported_and_unsupported_claims_are_distinguishable(self):
        with tempfile.TemporaryDirectory() as directory:
            programs, evidence, claims = self._stores(directory)
            try:
                admitted = self._admit(evidence, b"support")
                unsupported = claims.create("unsupported")
                supported = claims.create("supported")
                supported = claims.support(supported.claim_id, (admitted.evidence_id,))
                self.assertIs(unsupported.status, ClaimStatus.PROPOSED)
                self.assertIs(supported.status, ClaimStatus.SUPPORTED)
                with self.assertRaises(EvidenceMissing):
                    claims.verification_evidence(unsupported.claim_id)
                roots = claims.verification_evidence(supported.claim_id)
                self.assertEqual(
                    tuple(item.evidence_id for item in roots),
                    (admitted.evidence_id,),
                )
            finally:
                programs.close()

    def test_reference_only_does_not_make_claim_supported(self):
        with tempfile.TemporaryDirectory() as directory:
            programs, evidence, claims = self._stores(directory)
            try:
                admitted = self._admit(evidence, b"reference")
                claim = claims.create("reference only")
                claim = claims.add_reference(claim.claim_id, admitted.evidence_id)
                self.assertIs(claim.status, ClaimStatus.PROPOSED)
                with self.assertRaises(EvidenceMissing):
                    claims.verification_evidence(claim.claim_id)
            finally:
                programs.close()

    def test_contradiction_retains_prior_support_and_history(self):
        with tempfile.TemporaryDirectory() as directory:
            programs, evidence, claims = self._stores(directory)
            try:
                support = self._admit(evidence, b"support")
                contradiction = self._admit(evidence, b"contradiction")
                claim = claims.create("statement")
                claims.support(claim.claim_id, (support.evidence_id,))
                final = claims.contradict(
                    claim.claim_id,
                    (contradiction.evidence_id,),
                )
                self.assertIs(final.status, ClaimStatus.CONTRADICTED)
                history = claims.history(claim.claim_id)
                self.assertEqual(
                    tuple(item.status for item in history),
                    (
                        ClaimStatus.PROPOSED,
                        ClaimStatus.SUPPORTED,
                        ClaimStatus.CONTRADICTED,
                    ),
                )
                inputs = claims.disposition_inputs(claim.claim_id)
                self.assertEqual(
                    tuple(item.evidence_id for item in inputs.support_refs),
                    (support.evidence_id,),
                )
                self.assertEqual(
                    tuple(item.evidence_id for item in inputs.contradiction_refs),
                    (contradiction.evidence_id,),
                )
                with self.assertRaises(EvidenceMissing):
                    claims.verification_evidence(claim.claim_id)
            finally:
                programs.close()

    def test_claim_history_gap_is_rejected_against_semantic_events(self):
        with tempfile.TemporaryDirectory() as directory:
            programs, evidence, claims = self._stores(directory)
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
                    claims.history(claim.claim_id)
            finally:
                programs.close()

    def test_claim_identity_cannot_be_reused_when_projection_is_lost(self):
        with tempfile.TemporaryDirectory() as directory:
            programs, _, claims = self._stores(directory)
            try:
                claims.create("original", claim_id="c-fixed")
                programs._db.execute(
                    "DELETE FROM claim_projections WHERE claim_id = ?",
                    ("c-fixed",),
                )
                with self.assertRaises(PersistenceConflict):
                    claims.create("replacement", claim_id="c-fixed")
            finally:
                programs.close()

    def test_supersession_retains_old_claim_history(self):
        with tempfile.TemporaryDirectory() as directory:
            programs, _, claims = self._stores(directory)
            try:
                old = claims.create("old statement")
                successor = claims.create("new statement")
                final = claims.supersede(old.claim_id, successor.claim_id)
                self.assertIs(final.status, ClaimStatus.SUPERSEDED)
                history = claims.history(old.claim_id)
                self.assertEqual(history[0].status, ClaimStatus.PROPOSED)
                self.assertEqual(history[-1].status, ClaimStatus.SUPERSEDED)
                self.assertEqual(
                    claims.get(successor.claim_id).statement,
                    "new statement",
                )
            finally:
                programs.close()

    def test_historical_evidence_remains_historical_after_retrieval(self):
        with tempfile.TemporaryDirectory() as directory:
            programs, evidence, _ = self._stores(directory)
            try:
                admitted = self._admit(
                    evidence,
                    b"old",
                    currentness="historical",
                )
                self.assertEqual(
                    evidence.get(admitted.evidence_id).currentness,
                    "historical",
                )
                self.assertEqual(
                    evidence.reference(admitted.evidence_id).currentness,
                    "historical",
                )
                self.assertEqual(evidence.artifact(admitted.evidence_id), b"old")
                self.assertEqual(
                    evidence.get(admitted.evidence_id).currentness,
                    "historical",
                )
            finally:
                programs.close()

    def test_model_visible_evidence_reference_excludes_source_content(self):
        self.assertNotIn("content", {field.name for field in fields(EvidenceReference)})
        self.assertNotIn(
            "content_ref",
            {field.name for field in fields(EvidenceReference)},
        )

    def test_evidence_record_tampering_is_rejected_even_with_recomputed_record_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            programs, evidence, _ = self._stores(directory)
            try:
                admitted = self._admit(evidence, b"original")
                forged = replace(admitted, trust_class="forged")
                programs._db.execute(
                    """
                    UPDATE evidence_records
                    SET evidence_json = ?, evidence_record_digest = ?
                    WHERE evidence_id = ?
                    """,
                    (
                        record_to_json(forged),
                        canonical_digest(forged),
                        admitted.evidence_id,
                    ),
                )
                with self.assertRaises(IntegrityViolation):
                    evidence.get(admitted.evidence_id)
            finally:
                programs.close()

    def test_evidence_artifact_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            programs, evidence, _ = self._stores(directory)
            try:
                admitted = self._admit(evidence, b"original")
                artifact_path = (
                    Path(directory)
                    / "evidence"
                    / admitted.digest[:2]
                    / admitted.digest[2:]
                )
                artifact_path.write_bytes(b"forged!!")
                with self.assertRaises(IntegrityViolation):
                    evidence.get(admitted.evidence_id)
            finally:
                programs.close()

    def test_claim_projection_tampering_is_rejected_against_history_event(self):
        with tempfile.TemporaryDirectory() as directory:
            programs, _, claims = self._stores(directory)
            try:
                claim = claims.create("original")
                forged = replace(claim, statement="forged")
                programs._db.execute(
                    """
                    UPDATE claim_projections SET claim_json = ?, claim_digest = ?
                    WHERE claim_id = ?
                    """,
                    (
                        record_to_json(forged),
                        canonical_digest(forged),
                        claim.claim_id,
                    ),
                )
                with self.assertRaises(IntegrityViolation):
                    claims.get(claim.claim_id)
            finally:
                programs.close()

    def test_claim_link_tampering_is_rejected_against_semantic_event(self):
        with tempfile.TemporaryDirectory() as directory:
            programs, evidence, claims = self._stores(directory)
            try:
                admitted = self._admit(evidence, b"support")
                claim = claims.create("statement")
                claims.support(claim.claim_id, (admitted.evidence_id,))
                forged = ClaimEvidenceLink(
                    claim.claim_id,
                    admitted.evidence_id,
                    ClaimEvidenceRelation.REFERENCE,
                    "2026-08-31T00:00:01Z",
                )
                programs._db.execute(
                    """
                    UPDATE claim_evidence_links
                    SET relation = ?, link_json = ?, link_digest = ?
                    WHERE claim_id = ? AND evidence_id = ?
                    """,
                    (
                        forged.relation.value,
                        record_to_json(forged),
                        canonical_digest(forged),
                        claim.claim_id,
                        admitted.evidence_id,
                    ),
                )
                with self.assertRaises(IntegrityViolation):
                    claims.get(claim.claim_id)
            finally:
                programs.close()

    def test_disposition_inputs_contain_epistemic_inputs_not_authority(self):
        self.assertEqual(
            {field.name for field in fields(DispositionInputs)},
            {
                "claim_id",
                "claim_status",
                "support_refs",
                "contradiction_refs",
                "reference_refs",
            },
        )

    def test_evidence_and_claim_events_use_global_ledger_without_program_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            programs, evidence, claims = self._stores(directory)
            try:
                admitted = self._admit(evidence, b"support")
                claim = claims.create("statement")
                claims.support(claim.claim_id, (admitted.evidence_id,))
                rows = programs._db.execute(
                    """
                    SELECT event_type, program_id FROM events
                    WHERE event_type LIKE 'evidence.%' OR event_type LIKE 'claim.%'
                    ORDER BY sequence
                    """
                ).fetchall()
                self.assertEqual(
                    tuple(row["event_type"] for row in rows),
                    (
                        "evidence.admitted",
                        "claim.created",
                        "claim.supported",
                    ),
                )
                self.assertTrue(all(row["program_id"] is None for row in rows))
            finally:
                programs.close()


if __name__ == "__main__":
    unittest.main()
