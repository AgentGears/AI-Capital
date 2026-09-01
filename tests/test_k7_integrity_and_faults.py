from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_capital.kernel.claim_store import ClaimRepository
from ai_capital.kernel.completion import CompletionOracle
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import CompletionResult, ProgramStatus, VerificationResult
from ai_capital.kernel.errors import IntegrityViolation
from ai_capital.kernel.evidence_store import EvidenceRepository
from ai_capital.kernel.models import Program, WorkItem
from ai_capital.kernel.operation_journal import OperationJournal
from ai_capital.kernel.schema_codec import record_to_json
from ai_capital.kernel.serialization import canonical_digest
from ai_capital.kernel.verification import (
    VerificationObservation,
    VerificationRepository,
)


OBSERVED = "2026-09-01T00:00:00Z"
CRITERION = "artifact matches specification"


class FaultingProgramRepository(ProgramRepository):
    def __init__(self, database_path):
        self.fail_stage: str | None = None
        super().__init__(database_path)

    def _fault(self, stage: str) -> None:
        if stage == self.fail_stage:
            raise RuntimeError(f"injected fault: {stage}")


class PassingVerifier:
    def verify(self, contract, program, evidence_refs):
        return VerificationObservation(VerificationResult.PASS, "fixture_pass")


class ReadyFixture:
    def __init__(self, directory: str, *, faulting: bool = False):
        self.path = Path(directory) / "kernel.db"
        repository_type = FaultingProgramRepository if faulting else ProgramRepository
        self.programs = repository_type(self.path)
        program = self.programs.create(
            Program(
                "p-1",
                0,
                "completion integrity proof",
                work_items=(WorkItem("w-1", "complete required work"),),
                success_criteria=(CRITERION,),
            )
        )
        program = self.programs.transition(
            "p-1", ProgramStatus.ACTIVE, expected_revision=program.revision
        )
        program = self.programs.satisfy_work(
            "p-1", "w-1", expected_revision=program.revision
        )

        self.evidence = EvidenceRepository(self.programs)
        self.claims = ClaimRepository(self.programs, self.evidence)
        admitted = self.evidence.admit(
            content=b"verified",
            source_class="fixture_observation",
            observed_at=OBSERVED,
            provenance=("fixture:source", "admission:host"),
            trust_class="observed",
            currentness="current",
        )
        claim = self.claims.create("artifact matches specification")
        claim = self.claims.support(claim.claim_id, (admitted.evidence_id,))
        self.claim_id = claim.claim_id
        self.evidence_id = admitted.evidence_id

        self.operations = OperationJournal(self.programs)
        self.verifications = VerificationRepository(self.programs, self.claims)
        self.oracle = CompletionOracle(
            self.programs,
            self.verifications,
            self.operations,
        )
        self.contract = self.verifications.register_contract(
            program_id="p-1",
            success_criteria=(CRITERION,),
            required_claim_refs=(self.claim_id,),
            mandatory=True,
            require_effect_certainty=True,
        )
        current = self.programs.get("p-1")
        self.pending = self.oracle.enter_completion_pending(
            "p-1", expected_revision=current.revision
        )
        self.verification = self.verifications.run(
            self.contract.contract_id,
            expected_program_revision=self.pending.revision,
            verifier=PassingVerifier(),
        )

    def certify(self):
        return self.oracle.decide(
            "p-1", expected_revision=self.pending.revision
        )

    def close(self):
        self.programs.close()


class K7IntegrityAndFaultTests(unittest.TestCase):
    def test_missing_latest_verification_receipt_cannot_expose_older_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = ReadyFixture(directory)
            try:
                first = fx.verification
                second = fx.verifications.run(
                    fx.contract.contract_id,
                    expected_program_revision=fx.pending.revision,
                    verifier=PassingVerifier(),
                )
                self.assertNotEqual(first.verification_id, second.verification_id)
                fx.programs._db.execute(
                    "DELETE FROM verification_receipts WHERE verification_id = ?",
                    (second.verification_id,),
                )
                with self.assertRaises(IntegrityViolation):
                    fx.oracle.decide("p-1", expected_revision=fx.pending.revision)
            finally:
                fx.close()

    def test_missing_mandatory_contract_record_cannot_disappear_from_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = ReadyFixture(directory)
            try:
                fx.programs._db.execute(
                    "DELETE FROM verification_contracts WHERE contract_id = ?",
                    (fx.contract.contract_id,),
                )
                with self.assertRaises(IntegrityViolation):
                    fx.oracle.decide("p-1", expected_revision=fx.pending.revision)
            finally:
                fx.close()

    def test_contract_tamper_with_recomputed_digest_is_rejected_against_event(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = ReadyFixture(directory)
            try:
                forged = replace(fx.contract, mandatory=False)
                fx.programs._db.execute(
                    """
                    UPDATE verification_contracts
                    SET contract_json = ?, contract_digest = ?
                    WHERE contract_id = ?
                    """,
                    (
                        record_to_json(forged),
                        canonical_digest(forged),
                        fx.contract.contract_id,
                    ),
                )
                with self.assertRaises(IntegrityViolation):
                    fx.verifications.contract(fx.contract.contract_id)
            finally:
                fx.close()

    def test_verification_tamper_with_recomputed_digest_is_rejected_against_event(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = ReadyFixture(directory)
            try:
                forged = replace(fx.verification, evidence_refs=())
                fx.programs._db.execute(
                    """
                    UPDATE verification_receipts
                    SET verification_json = ?, verification_digest = ?
                    WHERE verification_id = ?
                    """,
                    (
                        record_to_json(forged),
                        canonical_digest(forged),
                        fx.verification.verification_id,
                    ),
                )
                with self.assertRaises(IntegrityViolation):
                    fx.verifications.get(fx.verification.verification_id)
            finally:
                fx.close()

    def test_verification_v1_to_v2_rebuilds_event_indexes(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = ReadyFixture(directory)
            contract_id = fx.contract.contract_id
            verification_id = fx.verification.verification_id
            try:
                fx.programs._db.execute(
                    "UPDATE component_schema SET version = 1 WHERE component = 'verification'"
                )
                fx.programs._db.execute("DROP TABLE verification_contract_event_index")
                fx.programs._db.execute("DROP TABLE verification_event_index")
            finally:
                fx.close()

            programs = ProgramRepository(Path(directory) / "kernel.db")
            try:
                evidence = EvidenceRepository(programs)
                claims = ClaimRepository(programs, evidence)
                verifications = VerificationRepository(programs, claims)
                self.assertEqual(verifications.contract(contract_id).contract_id, contract_id)
                self.assertEqual(
                    verifications.get(verification_id).verification_id,
                    verification_id,
                )
                version = programs._db.execute(
                    "SELECT version FROM component_schema WHERE component = 'verification'"
                ).fetchone()[0]
                self.assertEqual(int(version), 2)
            finally:
                programs.close()

    def test_future_verification_schema_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = ReadyFixture(directory)
            try:
                fx.programs._db.execute(
                    "UPDATE component_schema SET version = 99 WHERE component = 'verification'"
                )
            finally:
                fx.close()

            programs = ProgramRepository(Path(directory) / "kernel.db")
            try:
                evidence = EvidenceRepository(programs)
                claims = ClaimRepository(programs, evidence)
                with self.assertRaises(IntegrityViolation):
                    VerificationRepository(programs, claims)
            finally:
                programs.close()

    def test_completion_receipt_tamper_with_recomputed_digest_is_rejected_against_event(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = ReadyFixture(directory)
            try:
                receipt = fx.certify()
                forged = replace(receipt, rationale_codes=("forged",))
                fx.programs._db.execute(
                    """
                    UPDATE completion_receipts
                    SET receipt_json = ?, receipt_digest = ?
                    WHERE receipt_id = ?
                    """,
                    (
                        record_to_json(forged),
                        canonical_digest(forged),
                        receipt.receipt_id,
                    ),
                )
                with self.assertRaises(IntegrityViolation):
                    fx.oracle.receipt(receipt.receipt_id)
            finally:
                fx.close()

    def test_missing_completion_receipt_is_detected_by_decision_history(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = ReadyFixture(directory)
            try:
                receipt = fx.certify()
                fx.programs._db.execute(
                    "DELETE FROM completion_receipts WHERE receipt_id = ?",
                    (receipt.receipt_id,),
                )
                with self.assertRaises(IntegrityViolation):
                    fx.oracle.receipts_for_program("p-1")
            finally:
                fx.close()

    def test_completion_v1_to_v2_rebuilds_decision_index(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = ReadyFixture(directory)
            receipt = fx.certify()
            try:
                fx.programs._db.execute(
                    "UPDATE component_schema SET version = 1 WHERE component = 'completion'"
                )
                fx.programs._db.execute("DROP TABLE completion_decision_event_index")
            finally:
                fx.close()

            programs = ProgramRepository(Path(directory) / "kernel.db")
            try:
                evidence = EvidenceRepository(programs)
                claims = ClaimRepository(programs, evidence)
                operations = OperationJournal(programs)
                verifications = VerificationRepository(programs, claims)
                oracle = CompletionOracle(programs, verifications, operations)
                self.assertEqual(oracle.receipt(receipt.receipt_id), receipt)
                version = programs._db.execute(
                    "SELECT version FROM component_schema WHERE component = 'completion'"
                ).fetchone()[0]
                self.assertEqual(int(version), 2)
            finally:
                programs.close()

    def test_precommit_fault_after_decision_receipt_rolls_back_certification(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = ReadyFixture(directory, faulting=True)
            try:
                fx.programs.fail_stage = "completion_after_decision_receipt"
                with self.assertRaises(RuntimeError):
                    fx.certify()
                self.assertIs(
                    fx.programs.get("p-1").status,
                    ProgramStatus.COMPLETION_PENDING,
                )
                self.assertEqual(
                    fx.programs._db.execute(
                        "SELECT COUNT(*) FROM completion_receipts"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    fx.programs._db.execute(
                        "SELECT COUNT(*) FROM completion_decision_event_index"
                    ).fetchone()[0],
                    0,
                )
            finally:
                fx.close()

    def test_precommit_fault_after_program_event_rolls_back_everything(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = ReadyFixture(directory, faulting=True)
            try:
                fx.programs.fail_stage = "completion_after_program_event"
                with self.assertRaises(RuntimeError):
                    fx.certify()
                self.assertIs(
                    fx.programs.get("p-1").status,
                    ProgramStatus.COMPLETION_PENDING,
                )
                self.assertEqual(
                    fx.programs._db.execute(
                        "SELECT COUNT(*) FROM completion_receipts"
                    ).fetchone()[0],
                    0,
                )
            finally:
                fx.close()

    def test_precommit_fault_after_projection_write_rolls_back_everything(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = ReadyFixture(directory, faulting=True)
            try:
                fx.programs.fail_stage = "completion_after_projection_write"
                with self.assertRaises(RuntimeError):
                    fx.certify()
                self.assertIs(
                    fx.programs.get("p-1").status,
                    ProgramStatus.COMPLETION_PENDING,
                )
                fx.programs.verify_integrity("p-1")
                self.assertEqual(
                    fx.programs._db.execute(
                        "SELECT COUNT(*) FROM completion_receipts"
                    ).fetchone()[0],
                    0,
                )
            finally:
                fx.close()

    def test_postcommit_lost_ack_preserves_receipt_and_completed_program(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = ReadyFixture(directory, faulting=True)
            try:
                fx.programs.fail_stage = "completion_after_commit"
                with self.assertRaises(RuntimeError):
                    fx.certify()
                self.assertIs(fx.programs.get("p-1").status, ProgramStatus.COMPLETED)
                fx.programs.verify_integrity("p-1")
                row = fx.programs._db.execute(
                    "SELECT receipt_id FROM completion_decision_event_index"
                ).fetchone()
                self.assertIsNotNone(row)
                receipt = fx.oracle.receipt(str(row["receipt_id"]))
                self.assertIs(receipt.result, CompletionResult.CERTIFIED)
            finally:
                fx.close()


if __name__ == "__main__":
    unittest.main()
