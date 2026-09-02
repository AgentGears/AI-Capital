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
from ai_capital.kernel.enums import (
    CompletionResult,
    EffectClass,
    EffectStatus,
    ExecutionOutcome,
    ProgramStatus,
    ReconciliationStatus,
    VerificationResult,
)
from ai_capital.kernel.errors import IntegrityViolation, VerificationStale
from ai_capital.kernel.evidence_store import EvidenceRepository
from ai_capital.kernel.models import (
    CapabilityResolution,
    Program,
    ResolvedEffect,
    WorkItem,
)
from ai_capital.kernel.operation_journal import OperationJournal
from ai_capital.kernel.schema_codec import record_to_json
from ai_capital.kernel.serialization import canonical_digest
from ai_capital.kernel.verification import (
    VerificationObservation,
    VerificationRepository,
)


OBSERVED = "2026-09-01T00:00:00Z"
CRITERION = "artifact matches specification"
FINISHED = "2026-09-01T00:01:00Z"


class PassingVerifier:
    def verify(self, contract, program, evidence_refs):
        return VerificationObservation(VerificationResult.PASS, "fixture_pass")


class Fixture:
    def __init__(self, directory: str, *, require_effect_certainty: bool = True):
        self.programs = ProgramRepository(Path(directory) / "kernel.db")
        program = self.programs.create(
            Program(
                "p-1",
                0,
                "K7 review round-two proof",
                work_items=(WorkItem("w-1", "complete required work"),),
                success_criteria=(CRITERION,),
            )
        )
        program = self.programs.transition(
            "p-1", ProgramStatus.ACTIVE, expected_revision=program.revision
        )
        self.programs.satisfy_work(
            "p-1", "w-1", expected_revision=program.revision
        )

        self.evidence = EvidenceRepository(self.programs)
        self.claims = ClaimRepository(self.programs, self.evidence)
        admitted = self.evidence.admit(
            content=b"verified artifact",
            source_class="fixture_observation",
            observed_at=OBSERVED,
            provenance=("fixture:source", "admission:host"),
            trust_class="observed",
            currentness="current",
        )
        claim = self.claims.create(CRITERION)
        claim = self.claims.support(claim.claim_id, (admitted.evidence_id,))
        self.claim_id = claim.claim_id

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
            require_effect_certainty=require_effect_certainty,
        )

    def create_running_mutation(self):
        resolution = CapabilityResolution(
            request_id="request-mutation",
            capability_id="workspace.write",
            binding_revision=0,
            arguments={"path": "artifact.txt", "content": "updated"},
            resolved_effect=ResolvedEffect(
                resource_type="workspace_file",
                target="artifact.txt",
                effect_class=EffectClass.MODIFY,
                parameters={"path": "artifact.txt"},
            ),
        )
        operation = self.operations.create_intent(
            program_id="p-1",
            actor_id="actor-1",
            resolution=resolution,
            authority_receipt_ref="execution-authority-1",
        )
        self.operations.mark_admitted(operation.operation_id)
        return self.operations.mark_running(operation.operation_id)

    def enter_and_verify(self):
        current = self.programs.get("p-1")
        pending = self.oracle.enter_completion_pending(
            "p-1", expected_revision=current.revision
        )
        verification = self.verifications.run(
            self.contract.contract_id,
            expected_program_revision=pending.revision,
            verifier=PassingVerifier(),
        )
        return pending, verification

    def close(self):
        self.programs.close()


class K7ReviewRoundTwoTests(unittest.TestCase):
    def test_interrupted_mutation_stales_verification_even_without_effect_certainty_requirement(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = Fixture(directory, require_effect_certainty=False)
            try:
                fx.create_running_mutation()
                pending, verification = fx.enter_and_verify()

                recovered = fx.operations.recover_interrupted()
                self.assertEqual(len(recovered), 1)
                self.assertIs(recovered[0].execution_outcome, ExecutionOutcome.FAILED)
                self.assertIs(recovered[0].effect_status, EffectStatus.INDETERMINATE)

                with self.assertRaises(VerificationStale):
                    fx.verifications.current(verification.verification_id)

                receipt = fx.oracle.decide(
                    "p-1", expected_revision=pending.revision
                )
                self.assertIs(receipt.result, CompletionResult.REJECTED)
                self.assertIn(
                    f"verification_stale:{fx.contract.contract_id}",
                    receipt.rationale_codes,
                )
            finally:
                fx.close()

    def test_completion_rejects_forged_terminal_projection_when_latest_event_is_running(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = Fixture(directory)
            try:
                running = fx.create_running_mutation()
                pending, _ = fx.enter_and_verify()

                forged = replace(
                    running,
                    execution_outcome=ExecutionOutcome.FAILED,
                    effect_status=EffectStatus.ABSENT,
                    reconciliation_status=ReconciliationStatus.NOT_REQUIRED,
                    finished_at=FINISHED,
                )
                fx.programs._db.execute(
                    """
                    UPDATE operation_projections
                    SET operation_json = ?, operation_digest = ?
                    WHERE operation_id = ?
                    """,
                    (
                        record_to_json(forged),
                        canonical_digest(forged),
                        running.operation_id,
                    ),
                )

                with self.assertRaises(IntegrityViolation):
                    fx.oracle.decide("p-1", expected_revision=pending.revision)
            finally:
                fx.close()

    def test_completion_receipt_and_index_deletion_cannot_erase_decision_event(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = Fixture(directory)
            try:
                pending, _ = fx.enter_and_verify()
                receipt = fx.oracle.decide(
                    "p-1", expected_revision=pending.revision
                )
                self.assertIs(receipt.result, CompletionResult.CERTIFIED)

                fx.programs._db.execute(
                    "DELETE FROM completion_decision_event_index WHERE receipt_id = ?",
                    (receipt.receipt_id,),
                )
                fx.programs._db.execute(
                    "DELETE FROM completion_receipts WHERE receipt_id = ?",
                    (receipt.receipt_id,),
                )

                with self.assertRaises(IntegrityViolation):
                    fx.oracle.receipts_for_program("p-1")
            finally:
                fx.close()


if __name__ == "__main__":
    unittest.main()
