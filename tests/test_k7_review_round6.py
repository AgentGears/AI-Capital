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
from ai_capital.kernel.enums import ProgramStatus, VerificationResult
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


class K7ReviewRoundSixTests(unittest.TestCase):
    def test_forged_older_verification_receipt_blocks_later_certification(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "kernel.db")
            try:
                program = programs.create(
                    Program(
                        "p-1",
                        0,
                        "verification receipt history integrity proof",
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
                pending = oracle.enter_completion_pending(
                    "p-1", expected_revision=program.revision
                )

                older = verifications.run(
                    contract.contract_id,
                    expected_program_revision=pending.revision,
                    verifier=PassingVerifier(),
                )
                latest = verifications.run(
                    contract.contract_id,
                    expected_program_revision=pending.revision,
                    verifier=PassingVerifier(),
                )
                self.assertNotEqual(older.verification_id, latest.verification_id)
                self.assertEqual(
                    verifications.current(latest.verification_id),
                    latest,
                )

                forged = replace(older, evidence_refs=())
                programs._db.execute(
                    """
                    UPDATE verification_receipts
                    SET verification_json = ?, verification_digest = ?
                    WHERE verification_id = ?
                    """,
                    (
                        record_to_json(forged),
                        canonical_digest(forged),
                        older.verification_id,
                    ),
                )

                with self.assertRaises(IntegrityViolation):
                    oracle.decide(
                        "p-1", expected_revision=pending.revision
                    )
                self.assertIs(
                    programs.get("p-1").status,
                    ProgramStatus.COMPLETION_PENDING,
                )
            finally:
                programs.close()


if __name__ == "__main__":
    unittest.main()
