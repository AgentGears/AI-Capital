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
    EffectClass,
    EffectStatus,
    ExecutionOutcome,
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


class K7ReviewRoundSevenTests(unittest.TestCase):
    def _ready_with_confirmed_mutation(self, directory: str):
        programs = ProgramRepository(Path(directory) / "kernel.db")
        program = programs.create(
            Program(
                "p-1",
                0,
                "operation receipt integrity proof",
                work_items=(WorkItem("w-1", "complete required work"),),
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
            content=b"verified artifact",
            source_class="fixture_observation",
            observed_at=OBSERVED,
            provenance=("fixture:source", "admission:host"),
            trust_class="observed",
            currentness="current",
        )
        claim = claims.create(CRITERION)
        claim = claims.support(claim.claim_id, (admitted.evidence_id,))

        operations = OperationJournal(programs)
        resolution = CapabilityResolution(
            request_id="request-confirmed-mutation",
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
        operation = operations.create_intent(
            program_id="p-1",
            actor_id="actor-1",
            resolution=resolution,
            authority_receipt_ref="execution-authority-1",
        )
        operations.mark_admitted(operation.operation_id)
        operations.mark_running(operation.operation_id)
        operation = operations.finish(
            operation.operation_id,
            ExecutionObservation(
                execution_outcome=ExecutionOutcome.SUCCEEDED,
                effect_status=EffectStatus.CONFIRMED,
                output={"bytes_written": 7},
                backend_receipt_ref="backend-1",
            ),
        )

        verifications = VerificationRepository(programs, claims)
        oracle = CompletionOracle(programs, verifications, operations)
        contract = verifications.register_contract(
            program_id="p-1",
            success_criteria=(CRITERION,),
            required_claim_refs=(claim.claim_id,),
            mandatory=True,
            require_effect_certainty=True,
        )
        pending = oracle.enter_completion_pending(
            "p-1", expected_revision=program.revision
        )
        verifications.run(
            contract.contract_id,
            expected_program_revision=pending.revision,
            verifier=PassingVerifier(),
        )
        return programs, operations, oracle, operation, pending

    def test_missing_execution_receipt_blocks_certification(self):
        with tempfile.TemporaryDirectory() as directory:
            programs, _, oracle, operation, pending = self._ready_with_confirmed_mutation(
                directory
            )
            try:
                receipt_id = operation.receipt_refs[-1]
                programs._db.execute(
                    "DELETE FROM operation_receipts WHERE receipt_id = ?",
                    (receipt_id,),
                )

                with self.assertRaises(IntegrityViolation):
                    oracle.decide("p-1", expected_revision=pending.revision)
                self.assertIs(
                    programs.get("p-1").status,
                    ProgramStatus.COMPLETION_PENDING,
                )
            finally:
                programs.close()

    def test_forged_execution_receipt_body_blocks_certification(self):
        with tempfile.TemporaryDirectory() as directory:
            programs, operations, oracle, operation, pending = (
                self._ready_with_confirmed_mutation(directory)
            )
            try:
                receipt = operations.execution_receipt(operation.operation_id)
                forged = replace(receipt, backend_receipt_ref="forged-backend")
                programs._db.execute(
                    """
                    UPDATE operation_receipts
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
                    oracle.decide("p-1", expected_revision=pending.revision)
                self.assertIs(
                    programs.get("p-1").status,
                    ProgramStatus.COMPLETION_PENDING,
                )
            finally:
                programs.close()


if __name__ == "__main__":
    unittest.main()
