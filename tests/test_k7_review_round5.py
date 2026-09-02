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


class PassingVerifier:
    def verify(self, contract, program, evidence_refs):
        return VerificationObservation(VerificationResult.PASS, "fixture_pass")


class K7ReviewRoundFiveTests(unittest.TestCase):
    def test_forged_prior_receipt_body_blocks_later_certification(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "kernel.db")
            try:
                program = programs.create(
                    Program(
                        "p-1",
                        0,
                        "completion receipt body integrity proof",
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
                verifications = VerificationRepository(programs, claims)
                oracle = CompletionOracle(programs, verifications, operations)
                contract = verifications.register_contract(
                    program_id="p-1",
                    success_criteria=(CRITERION,),
                    required_claim_refs=(claim.claim_id,),
                    mandatory=True,
                    require_effect_certainty=True,
                )

                blocker = oracle.open_blocker(
                    "p-1",
                    code="fixture_blocker",
                    detail="force an initial rejection",
                )
                pending = oracle.enter_completion_pending(
                    "p-1", expected_revision=program.revision
                )
                verifications.run(
                    contract.contract_id,
                    expected_program_revision=pending.revision,
                    verifier=PassingVerifier(),
                )
                rejected = oracle.decide(
                    "p-1", expected_revision=pending.revision
                )
                self.assertIs(rejected.result, CompletionResult.REJECTED)
                self.assertIs(programs.get("p-1").status, ProgramStatus.ACTIVE)

                forged = replace(
                    rejected,
                    rationale_codes=("forged_prior_rejection",),
                )
                programs._db.execute(
                    """
                    UPDATE completion_receipts
                    SET receipt_json = ?, receipt_digest = ?
                    WHERE receipt_id = ?
                    """,
                    (
                        record_to_json(forged),
                        canonical_digest(forged),
                        rejected.receipt_id,
                    ),
                )

                oracle.resolve_blocker(blocker.blocker_id)
                active = programs.get("p-1")
                second_pending = oracle.enter_completion_pending(
                    "p-1", expected_revision=active.revision
                )
                verifications.run(
                    contract.contract_id,
                    expected_program_revision=second_pending.revision,
                    verifier=PassingVerifier(),
                )

                with self.assertRaises(IntegrityViolation):
                    oracle.decide(
                        "p-1", expected_revision=second_pending.revision
                    )
                self.assertIs(
                    programs.get("p-1").status,
                    ProgramStatus.COMPLETION_PENDING,
                )
            finally:
                programs.close()


if __name__ == "__main__":
    unittest.main()
