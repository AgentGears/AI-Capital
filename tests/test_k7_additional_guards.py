from __future__ import annotations

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
    ProgramStatus,
    VerificationResult,
)
from ai_capital.kernel.errors import IntegrityViolation
from ai_capital.kernel.evidence_store import EvidenceRepository
from ai_capital.kernel.models import (
    CapabilityResolution,
    Program,
    ResolvedEffect,
    WorkItem,
)
from ai_capital.kernel.operation_journal import OperationJournal
from ai_capital.kernel.verification import VerificationObservation, VerificationRepository


OBSERVED = "2026-09-01T00:00:00Z"
CRITERION = "result independently checked"


class ResultVerifier:
    def __init__(self, result: VerificationResult):
        self.result = result

    def verify(self, contract, program, evidence_refs):
        return VerificationObservation(self.result, f"fixture_{self.result.value}")


class K7AdditionalGuardTests(unittest.TestCase):
    def _ready(self, directory: str):
        programs = ProgramRepository(Path(directory) / "kernel.db")
        program = programs.create(
            Program(
                "p-1",
                0,
                "additional completion guards",
                work_items=(WorkItem("w-1", "finish"),),
                success_criteria=(CRITERION,),
            )
        )
        program = programs.transition(
            "p-1", ProgramStatus.ACTIVE, expected_revision=program.revision
        )
        program = programs.satisfy_work(
            "p-1", "w-1", expected_revision=program.revision
        )
        evidence = EvidenceRepository(programs)
        claims = ClaimRepository(programs, evidence)
        admitted = evidence.admit(
            content=b"independent-check",
            source_class="fixture_observation",
            observed_at=OBSERVED,
            provenance=("fixture:source", "admission:host"),
            trust_class="observed",
            currentness="current",
        )
        claim = claims.create("result independently checked")
        claim = claims.support(claim.claim_id, (admitted.evidence_id,))
        operations = OperationJournal(programs)
        verifications = VerificationRepository(programs, claims)
        oracle = CompletionOracle(programs, verifications, operations)
        contract = verifications.register_contract(
            program_id="p-1",
            success_criteria=(CRITERION,),
            required_claim_refs=(claim.claim_id,),
            mandatory=True,
            require_effect_certainty=True,
        )
        current = programs.get("p-1")
        pending = oracle.enter_completion_pending(
            "p-1", expected_revision=current.revision
        )
        return programs, operations, verifications, oracle, contract, pending

    def test_lost_operation_projection_cannot_erase_requested_operation_from_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            programs, operations, verifications, oracle, contract, pending = self._ready(directory)
            try:
                verifications.run(
                    contract.contract_id,
                    expected_program_revision=pending.revision,
                    verifier=ResultVerifier(VerificationResult.PASS),
                )
                resolution = CapabilityResolution(
                    request_id="request-lost-projection",
                    capability_id="workspace.write",
                    binding_revision=0,
                    arguments={"path": "result.txt", "content": "x"},
                    resolved_effect=ResolvedEffect(
                        resource_type="workspace",
                        target="result.txt",
                        effect_class=EffectClass.MODIFY,
                        parameters={"path": "result.txt"},
                    ),
                )
                operation = operations.create_intent(
                    program_id="p-1",
                    actor_id="a-1",
                    resolution=resolution,
                    authority_receipt_ref="authority-fixture",
                )
                programs._db.execute(
                    "DELETE FROM operation_projections WHERE operation_id = ?",
                    (operation.operation_id,),
                )
                with self.assertRaises(IntegrityViolation):
                    oracle.decide("p-1", expected_revision=pending.revision)
            finally:
                programs.close()

    def test_newer_current_fail_dominates_older_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            programs, _, verifications, oracle, contract, pending = self._ready(directory)
            try:
                passed = verifications.run(
                    contract.contract_id,
                    expected_program_revision=pending.revision,
                    verifier=ResultVerifier(VerificationResult.PASS),
                )
                failed = verifications.run(
                    contract.contract_id,
                    expected_program_revision=pending.revision,
                    verifier=ResultVerifier(VerificationResult.FAIL),
                )
                self.assertNotEqual(passed.verification_id, failed.verification_id)
                receipt = oracle.decide("p-1", expected_revision=pending.revision)
                self.assertIs(receipt.result, CompletionResult.REJECTED)
                self.assertEqual(receipt.verification_refs, (failed.verification_id,))
                self.assertIn(
                    f"verification_failed:{contract.contract_id}",
                    receipt.rationale_codes,
                )
            finally:
                programs.close()


if __name__ == "__main__":
    unittest.main()
