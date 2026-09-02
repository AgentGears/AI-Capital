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
from ai_capital.kernel.operation_journal import ExecutionObservation, OperationJournal
from ai_capital.kernel.schema_codec import record_to_json
from ai_capital.kernel.serialization import canonical_digest
from ai_capital.kernel.verification import (
    VerificationObservation,
    VerificationRepository,
)


OBSERVED = "2026-09-01T00:00:00Z"
CRITERION = "artifact matches specification"


class PassingVerifier:
    def verify(self, contract, program, evidence_refs):
        return VerificationObservation(VerificationResult.PASS, "fixture_pass")


class Fixture:
    def __init__(self, directory: str):
        self.programs = ProgramRepository(Path(directory) / "kernel.db")
        program = self.programs.create(
            Program(
                "p-1",
                0,
                "K7 review remediation proof",
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
            require_effect_certainty=True,
        )

    def create_mutating_operation(self):
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
        return self.operations.create_intent(
            program_id="p-1",
            actor_id="actor-1",
            resolution=resolution,
            authority_receipt_ref="execution-authority-1",
        )

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


class K7ReviewRoundOneTests(unittest.TestCase):
    def test_later_confirmed_mutation_stales_prior_pass_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = Fixture(directory)
            try:
                operation = fx.create_mutating_operation()
                fx.operations.mark_admitted(operation.operation_id)
                fx.operations.mark_running(operation.operation_id)
                pending, verification = fx.enter_and_verify()

                fx.operations.finish(
                    operation.operation_id,
                    ExecutionObservation(
                        execution_outcome=ExecutionOutcome.SUCCEEDED,
                        effect_status=EffectStatus.CONFIRMED,
                        output={"bytes_written": 7},
                        backend_receipt_ref="backend-1",
                    ),
                )

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
                self.assertIs(fx.programs.get("p-1").status, ProgramStatus.ACTIVE)
            finally:
                fx.close()

    def test_completion_rejects_projection_effect_rewrite_against_requested_event(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = Fixture(directory)
            try:
                operation = fx.create_mutating_operation()
                pending, _ = fx.enter_and_verify()
                original_resolution = fx.operations.resolution(operation.operation_id)
                forged_resolution = replace(
                    original_resolution,
                    resolved_effect=replace(
                        original_resolution.resolved_effect,
                        effect_class=EffectClass.OBSERVE,
                    ),
                )
                forged_operation = replace(
                    fx.operations.get(operation.operation_id),
                    request_digest=canonical_digest(forged_resolution),
                )
                fx.programs._db.execute(
                    """
                    UPDATE operation_projections
                    SET operation_json = ?, operation_digest = ?,
                        resolution_json = ?, resolution_digest = ?
                    WHERE operation_id = ?
                    """,
                    (
                        record_to_json(forged_operation),
                        canonical_digest(forged_operation),
                        record_to_json(forged_resolution),
                        canonical_digest(forged_resolution),
                        operation.operation_id,
                    ),
                )

                with self.assertRaises(IntegrityViolation):
                    fx.oracle.decide("p-1", expected_revision=pending.revision)
            finally:
                fx.close()

    def test_contract_and_index_deletion_cannot_erase_mandatory_event_obligation(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = Fixture(directory)
            try:
                missing_contract = fx.verifications.register_contract(
                    program_id="p-1",
                    success_criteria=(CRITERION,),
                    required_claim_refs=(fx.claim_id,),
                    mandatory=True,
                    require_effect_certainty=True,
                )
                pending, _ = fx.enter_and_verify()

                fx.programs._db.execute(
                    "DELETE FROM verification_contract_event_index WHERE contract_id = ?",
                    (missing_contract.contract_id,),
                )
                fx.programs._db.execute(
                    "DELETE FROM verification_contracts WHERE contract_id = ?",
                    (missing_contract.contract_id,),
                )

                with self.assertRaises(IntegrityViolation):
                    fx.oracle.decide("p-1", expected_revision=pending.revision)
            finally:
                fx.close()


if __name__ == "__main__":
    unittest.main()
