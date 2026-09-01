from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_capital.kernel.claim_store import ClaimRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import (
    EffectClass,
    EffectStatus,
    ExecutionOutcome,
    ProgramStatus,
    VerificationResult,
)
from ai_capital.kernel.errors import (
    InvalidRequest,
    InvalidStateTransition,
    VerificationStale,
)
from ai_capital.kernel.evidence_store import EvidenceRepository
from ai_capital.kernel.models import (
    CapabilityResolution,
    Program,
    ResolvedEffect,
)
from ai_capital.kernel.operation_journal import ExecutionObservation, OperationJournal
from ai_capital.kernel.verification import (
    VerificationObservation,
    VerificationRepository,
)


OBSERVED = "2026-09-01T00:00:00Z"
CRITERION = "artifact matches specification"


class MutatingVerifier:
    def __init__(self, callback):
        self._callback = callback

    def verify(self, contract, program, evidence_refs):
        self._callback()
        return VerificationObservation(VerificationResult.PASS, "fixture_pass")


class K7ReviewRoundFourTests(unittest.TestCase):
    def test_protected_mutation_during_verifier_execution_blocks_admission(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "kernel.db")
            try:
                program = programs.create(
                    Program(
                        "p-1",
                        0,
                        "verification evaluation fence",
                        success_criteria=(CRITERION,),
                    )
                )
                program = programs.transition(
                    "p-1", ProgramStatus.ACTIVE, expected_revision=program.revision
                )

                evidence = EvidenceRepository(programs)
                claims = ClaimRepository(programs, evidence)
                admitted = evidence.admit(
                    content=b"pre-mutation artifact",
                    source_class="fixture_observation",
                    observed_at=OBSERVED,
                    provenance=("fixture:source", "admission:host"),
                    trust_class="observed",
                    currentness="current",
                )
                claim = claims.create(CRITERION)
                claim = claims.support(claim.claim_id, (admitted.evidence_id,))

                operations = OperationJournal(programs)
                verifications = VerificationRepository(programs, claims)
                contract = verifications.register_contract(
                    program_id="p-1",
                    success_criteria=(CRITERION,),
                    required_claim_refs=(claim.claim_id,),
                    mandatory=True,
                    require_effect_certainty=True,
                )
                pending = programs.transition(
                    "p-1",
                    ProgramStatus.COMPLETION_PENDING,
                    expected_revision=program.revision,
                )

                def mutate_during_verification() -> None:
                    resolution = CapabilityResolution(
                        request_id="request-during-verification",
                        capability_id="workspace.write",
                        binding_revision=0,
                        arguments={"path": "artifact.txt", "content": "changed"},
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
                    operations.finish(
                        operation.operation_id,
                        ExecutionObservation(
                            execution_outcome=ExecutionOutcome.SUCCEEDED,
                            effect_status=EffectStatus.CONFIRMED,
                            output={"bytes_written": 7},
                            backend_receipt_ref="backend-1",
                        ),
                    )

                with self.assertRaises(VerificationStale):
                    verifications.run(
                        contract.contract_id,
                        expected_program_revision=pending.revision,
                        verifier=MutatingVerifier(mutate_during_verification),
                    )

                count = programs._db.execute(
                    "SELECT COUNT(*) FROM verification_receipts WHERE contract_id = ?",
                    (contract.contract_id,),
                ).fetchone()[0]
                self.assertEqual(int(count), 0)
                self.assertIs(
                    programs.get("p-1").status,
                    ProgramStatus.COMPLETION_PENDING,
                )
            finally:
                programs.close()

    def test_oracle_reservation_preserves_preexisting_invalid_transition_error(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "kernel.db")
            try:
                created = programs.create(Program("p-1", 0, "transition contract"))
                with self.assertRaises(InvalidStateTransition):
                    programs.transition(
                        "p-1",
                        ProgramStatus.COMPLETED,
                        expected_revision=created.revision,
                    )

                active = programs.transition(
                    "p-1", ProgramStatus.ACTIVE, expected_revision=created.revision
                )
                pending = programs.transition(
                    "p-1",
                    ProgramStatus.COMPLETION_PENDING,
                    expected_revision=active.revision,
                )
                with self.assertRaises(InvalidRequest):
                    programs.transition(
                        "p-1",
                        ProgramStatus.COMPLETED,
                        expected_revision=pending.revision,
                    )
                self.assertEqual(programs.get("p-1"), pending)
            finally:
                programs.close()


if __name__ == "__main__":
    unittest.main()
