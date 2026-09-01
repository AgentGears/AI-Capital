from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_capital.kernel.actor_store import ActorRepository
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
from ai_capital.kernel.evidence_store import EvidenceRepository
from ai_capital.kernel.models import (
    Actor,
    CapabilityResolution,
    Program,
    ResolvedEffect,
    WorkItem,
)
from ai_capital.kernel.operation_journal import ExecutionObservation, OperationJournal
from ai_capital.kernel.verification import (
    VerificationObservation,
    VerificationRepository,
)


OBSERVED = "2026-09-01T00:00:00Z"
CRITERION = "required artifact is correct"


class StaticVerifier:
    def __init__(self, result: VerificationResult = VerificationResult.PASS):
        self.result = result
        self.calls = 0
        self.evidence_seen: tuple[str, ...] | None = None

    def verify(self, contract, program, evidence_refs):
        self.calls += 1
        self.evidence_seen = evidence_refs
        return VerificationObservation(self.result, f"fixture_{self.result.value}")


class Fixture:
    def __init__(self, directory: str, *, satisfy_work: bool):
        self.path = Path(directory) / "kernel.db"
        self.programs = ProgramRepository(self.path)
        program = self.programs.create(
            Program(
                "p-1",
                0,
                "verification proof",
                work_items=(WorkItem("w-1", "finish required work"),),
                success_criteria=(CRITERION,),
            )
        )
        program = self.programs.transition(
            program.program_id,
            ProgramStatus.ACTIVE,
            expected_revision=program.revision,
        )
        if satisfy_work:
            program = self.programs.satisfy_work(
                program.program_id,
                "w-1",
                expected_revision=program.revision,
            )
        self.program = program

        self.evidence = EvidenceRepository(self.programs)
        self.claims = ClaimRepository(self.programs, self.evidence)
        self.operations = OperationJournal(self.programs)
        self.verifications = VerificationRepository(self.programs, self.claims)
        self.oracle = CompletionOracle(
            self.programs,
            self.verifications,
            self.operations,
        )

        admitted = self.evidence.admit(
            content=b"verified-source",
            source_class="fixture_observation",
            observed_at=OBSERVED,
            provenance=("fixture:source", "admission:host"),
            trust_class="observed",
            currentness="current",
        )
        claim = self.claims.create("required artifact is correct")
        claim = self.claims.support(claim.claim_id, (admitted.evidence_id,))
        self.evidence_id = admitted.evidence_id
        self.claim_id = claim.claim_id
        self.contract = self.verifications.register_contract(
            program_id="p-1",
            success_criteria=(CRITERION,),
            required_claim_refs=(claim.claim_id,),
            mandatory=True,
            require_effect_certainty=True,
        )

    def enter_pending(self):
        current = self.programs.get("p-1")
        return self.oracle.enter_completion_pending(
            "p-1",
            expected_revision=current.revision,
        )

    def verify_pass(self, pending):
        verifier = StaticVerifier()
        verification = self.verifications.run(
            self.contract.contract_id,
            expected_program_revision=pending.revision,
            verifier=verifier,
        )
        return verifier, verification

    def add_indeterminate_mutation(self):
        resolution = CapabilityResolution(
            request_id="req-mutation",
            capability_id="workspace.write",
            binding_revision=0,
            arguments={"path": "notes.txt", "content": "updated"},
            resolved_effect=ResolvedEffect(
                resource_type="workspace",
                target="notes.txt",
                effect_class=EffectClass.MODIFY,
                parameters={"path": "notes.txt"},
            ),
        )
        operation = self.operations.create_intent(
            program_id="p-1",
            actor_id="a-1",
            resolution=resolution,
            authority_receipt_ref="authority-fixture",
        )
        self.operations.mark_admitted(operation.operation_id)
        self.operations.mark_running(operation.operation_id)
        return self.operations.finish(
            operation.operation_id,
            ExecutionObservation(
                execution_outcome=ExecutionOutcome.TIMED_OUT,
                effect_status=EffectStatus.INDETERMINATE,
                output={},
                error_code="lost_acknowledgement",
            ),
        )

    def close(self):
        self.programs.close()


class K7VerificationCompletionTests(unittest.TestCase):
    def test_model_done_with_outstanding_work_is_rejected_even_after_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = Fixture(directory, satisfy_work=False)
            try:
                pending = fx.enter_pending()
                _, verification = fx.verify_pass(pending)
                receipt = fx.oracle.decide("p-1", expected_revision=pending.revision)
                self.assertIs(receipt.result, CompletionResult.REJECTED)
                self.assertIn(
                    "required_work_outstanding:w-1",
                    receipt.rationale_codes,
                )
                self.assertIn(verification.verification_id, receipt.verification_refs)
                self.assertIs(fx.programs.get("p-1").status, ProgramStatus.ACTIVE)
            finally:
                fx.close()

    def test_current_pass_certifies_exact_program_revision_and_evidence_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = Fixture(directory, satisfy_work=True)
            try:
                pending = fx.enter_pending()
                verifier, verification = fx.verify_pass(pending)
                self.assertEqual(verifier.evidence_seen, (fx.evidence_id,))
                self.assertEqual(verification.evidence_refs, (fx.evidence_id,))

                receipt = fx.oracle.decide("p-1", expected_revision=pending.revision)
                self.assertIs(receipt.result, CompletionResult.CERTIFIED)
                self.assertEqual(receipt.program_revision, pending.revision)
                self.assertEqual(receipt.verification_refs, (verification.verification_id,))
                self.assertEqual(receipt.operation_refs, ())
                completed = fx.programs.get("p-1")
                self.assertIs(completed.status, ProgramStatus.COMPLETED)
                self.assertEqual(completed.revision, pending.revision + 1)
            finally:
                fx.close()

    def test_stale_verification_cannot_certify_newer_program_state(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = Fixture(directory, satisfy_work=True)
            try:
                first_pending = fx.enter_pending()
                _, verification = fx.verify_pass(first_pending)
                active = fx.programs.transition(
                    "p-1",
                    ProgramStatus.ACTIVE,
                    expected_revision=first_pending.revision,
                )
                second_pending = fx.oracle.enter_completion_pending(
                    "p-1",
                    expected_revision=active.revision,
                )
                receipt = fx.oracle.decide(
                    "p-1",
                    expected_revision=second_pending.revision,
                )
                self.assertIs(receipt.result, CompletionResult.REJECTED)
                self.assertIn(
                    f"verification_stale:{fx.contract.contract_id}",
                    receipt.rationale_codes,
                )
                self.assertIn(verification.verification_id, receipt.verification_refs)
            finally:
                fx.close()

    def test_claim_contradiction_invalidates_prior_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = Fixture(directory, satisfy_work=True)
            try:
                pending = fx.enter_pending()
                fx.verify_pass(pending)
                contradiction = fx.evidence.admit(
                    content=b"contradicting-source",
                    source_class="fixture_observation",
                    observed_at=OBSERVED,
                    provenance=("fixture:contradiction", "admission:host"),
                    trust_class="observed",
                    currentness="current",
                )
                fx.claims.contradict(fx.claim_id, (contradiction.evidence_id,))
                receipt = fx.oracle.decide("p-1", expected_revision=pending.revision)
                self.assertIs(receipt.result, CompletionResult.REJECTED)
                self.assertIn(
                    f"verification_stale:{fx.contract.contract_id}",
                    receipt.rationale_codes,
                )
            finally:
                fx.close()

    def test_indeterminate_protected_effect_blocks_completion_when_certainty_required(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = Fixture(directory, satisfy_work=True)
            try:
                operation = fx.add_indeterminate_mutation()
                pending = fx.enter_pending()
                fx.verify_pass(pending)
                receipt = fx.oracle.decide("p-1", expected_revision=pending.revision)
                self.assertIs(receipt.result, CompletionResult.REJECTED)
                self.assertIn(
                    f"protected_effect_indeterminate:{operation.operation_id}",
                    receipt.rationale_codes,
                )
                self.assertIn(operation.operation_id, receipt.operation_refs)
            finally:
                fx.close()

    def test_replacement_actor_cannot_bypass_completion_predicate(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = Fixture(directory, satisfy_work=False)
            try:
                actors = ActorRepository(fx.programs)
                actors.register(Actor("a-1", 0, "worker", "binding-a"))
                replacement = actors.replace_binding(
                    "a-1",
                    "binding-b",
                    expected_generation=0,
                )
                self.assertEqual(replacement.generation, 1)
                pending = fx.enter_pending()
                fx.verify_pass(pending)
                receipt = fx.oracle.decide("p-1", expected_revision=pending.revision)
                self.assertIs(receipt.result, CompletionResult.REJECTED)
                self.assertIn(
                    "required_work_outstanding:w-1",
                    receipt.rationale_codes,
                )
            finally:
                fx.close()

    def test_unresolved_explicit_blocker_rejects_then_resolution_requires_fresh_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = Fixture(directory, satisfy_work=True)
            try:
                blocker = fx.oracle.open_blocker(
                    "p-1",
                    code="human_review_pending",
                    detail="independent review has not closed",
                )
                pending = fx.enter_pending()
                fx.verify_pass(pending)
                rejected = fx.oracle.decide("p-1", expected_revision=pending.revision)
                self.assertIs(rejected.result, CompletionResult.REJECTED)
                self.assertTrue(
                    any(
                        code.startswith("completion_blocker:human_review_pending:")
                        for code in rejected.rationale_codes
                    )
                )
                fx.oracle.resolve_blocker(blocker.blocker_id)
                second_pending = fx.enter_pending()
                _, fresh = fx.verify_pass(second_pending)
                certified = fx.oracle.decide(
                    "p-1",
                    expected_revision=second_pending.revision,
                )
                self.assertIs(certified.result, CompletionResult.CERTIFIED)
                self.assertEqual(certified.verification_refs, (fresh.verification_id,))
            finally:
                fx.close()

    def test_completion_receipt_reconstructs_after_restart_without_transcript(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = Fixture(directory, satisfy_work=True)
            try:
                pending = fx.enter_pending()
                _, verification = fx.verify_pass(pending)
                expected = fx.oracle.decide("p-1", expected_revision=pending.revision)
                self.assertIs(expected.result, CompletionResult.CERTIFIED)
                receipt_id = expected.receipt_id
                verification_id = verification.verification_id
            finally:
                fx.close()

            disposable_transcript = {"assistant": "done", "context": "discarded"}
            del disposable_transcript

            programs = ProgramRepository(Path(directory) / "kernel.db")
            try:
                evidence = EvidenceRepository(programs)
                claims = ClaimRepository(programs, evidence)
                operations = OperationJournal(programs)
                verifications = VerificationRepository(programs, claims)
                oracle = CompletionOracle(programs, verifications, operations)
                restored = oracle.receipt(receipt_id)
                self.assertEqual(restored, expected)
                self.assertEqual(restored.verification_refs, (verification_id,))
                self.assertIs(programs.get("p-1").status, ProgramStatus.COMPLETED)
                programs.verify_integrity("p-1")
            finally:
                programs.close()


if __name__ == "__main__":
    unittest.main()
