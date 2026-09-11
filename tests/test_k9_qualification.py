from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_capital.kernel.actor_store import ActorRepository
from ai_capital.kernel.authority import AuthorityEngine, PolicySnapshot
from ai_capital.kernel.authority_store import AuthorityRepository
from ai_capital.kernel.bounded_inference import BoundedInferenceHost
from ai_capital.kernel.builtin_capabilities import install_builtin_capabilities
from ai_capital.kernel.capability_broker import CapabilityBroker, CapabilityHandlerRegistry
from ai_capital.kernel.capability_store import CapabilityRepository
from ai_capital.kernel.claim_store import ClaimRepository
from ai_capital.kernel.completion import CompletionOracle
from ai_capital.kernel.context import ContextCompiler, ContextRepository
from ai_capital.kernel.deterministic_provider import DeterministicInferenceProvider
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import (
    AuthorityDecisionKind,
    CompletionResult,
    ContextCompleteness,
    ContextPriority,
    EffectClass,
    EffectStatus,
    ExecutionOutcome,
    ProgramStatus,
    ReconciliationStatus,
    VerificationResult,
    WorkItemStatus,
)
from ai_capital.kernel.errors import AuthorityDenied, ExecutionTimeout
from ai_capital.kernel.evidence_store import EvidenceRepository
from ai_capital.kernel.inference import InferenceHost, ModelBindingRegistry
from ai_capital.kernel.models import Actor, CapabilityRequest, ContextReceipt, Grant, Program, WorkItem
from ai_capital.kernel.operation_journal import (
    ExecutionObservation,
    OperationHost,
    OperationJournal,
    ReconciliationObservation,
)
from ai_capital.kernel.verification import VerificationObservation, VerificationRepository


NOW = "2026-09-11T00:00:00Z"
OBSERVED = "2026-09-11T00:00:00Z"
CRITERION = "required artifact is correct"
ROOT = Path(__file__).resolve().parents[1]


def _context_receipt(program: Program, identity: str) -> ContextReceipt:
    return ContextReceipt(
        identity,
        program.program_id,
        program.revision,
        (f"program:{program.program_id}",),
        (),
        ContextCompleteness.COMPLETE,
        100,
        NOW,
    )


def _run_child(source: str, *args: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    current = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(ROOT / "src") + (os.pathsep + current if current else "")
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source), *(str(arg) for arg in args)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


class _StaticVerifier:
    def verify(self, contract, program, evidence_refs):
        return VerificationObservation(VerificationResult.PASS, "k9_fixture_pass")


class _TimeoutExecutor:
    supports_idempotency = False

    def __init__(self):
        self.calls = 0

    def execute(self, effect, *, idempotency_key):
        self.calls += 1
        raise ExecutionTimeout("acknowledgement lost after possible mutation")


class _ConfirmingReconciler:
    def __init__(self):
        self.calls = 0

    def reconcile(self, effect, *, execution_receipt, idempotency_key):
        self.calls += 1
        return ReconciliationObservation(
            EffectStatus.CONFIRMED,
            "effect_observed_during_reconciliation",
            ("evidence:k9-reconciliation",),
        )


class K9QualificationTests(unittest.TestCase):
    @staticmethod
    def _authorized_write_stack(directory: str, *, program_id: str = "p-1"):
        programs = ProgramRepository(Path(directory) / "kernel.db")
        program = programs.create(Program(program_id, 0, "K9 protected-effect qualification"))
        program = programs.transition(
            program.program_id,
            ProgramStatus.ACTIVE,
            expected_revision=program.revision,
        )
        actors = ActorRepository(programs)
        actors.register(Actor("a-1", 0, "worker", "binding-a"))
        capabilities = CapabilityRepository(programs)
        handlers = CapabilityHandlerRegistry()
        install_builtin_capabilities(capabilities, handlers)
        broker = CapabilityBroker(capabilities, handlers)
        authority_store = AuthorityRepository(programs)
        authority_store.install_policy(PolicySnapshot(0, (), (), NOW))
        authority_store.issue_grant(
            Grant(
                "g-1",
                "actor:a-1",
                ("workspace.write",),
                ("*",),
                EffectClass.MODIFY,
                (),
                NOW,
                None,
                0,
            )
        )
        authority = AuthorityEngine(programs, actors, capabilities, authority_store)
        snapshot = broker.snapshot(("workspace.write",))
        resolution = broker.resolve(
            CapabilityRequest(
                "req-1",
                "workspace.write",
                {"path": "notes.txt", "content": "updated"},
                0,
            ),
            snapshot=snapshot,
        )
        decision = authority.decide(
            program_id=program.program_id,
            actor_id="a-1",
            resolution=resolution,
        )
        receipt = authority.issue_execution_authority(
            decision_id=decision.decision.decision_id
        )
        journal = OperationJournal(programs)
        host = OperationHost(journal, authority)
        return programs, program, resolution, receipt, journal, host

    @staticmethod
    def _verification_stack(directory: str, *, satisfy_work: bool):
        programs = ProgramRepository(Path(directory) / "kernel.db")
        program = programs.create(
            Program(
                "p-1",
                0,
                "K9 completion qualification",
                work_items=(WorkItem("w-1", "finish required work"),),
                success_criteria=(CRITERION,),
            )
        )
        program = programs.transition(
            program.program_id,
            ProgramStatus.ACTIVE,
            expected_revision=program.revision,
        )
        if satisfy_work:
            program = programs.satisfy_work(
                program.program_id,
                "w-1",
                expected_revision=program.revision,
            )
        evidence = EvidenceRepository(programs)
        claims = ClaimRepository(programs, evidence)
        operations = OperationJournal(programs)
        verifications = VerificationRepository(programs, claims)
        oracle = CompletionOracle(programs, verifications, operations)
        source_bytes = b"K9 exact verification source bytes"
        admitted = evidence.admit(
            content=source_bytes,
            source_class="qualification_observation",
            observed_at=OBSERVED,
            provenance=("qualification:source", "admission:host"),
            trust_class="observed",
            currentness="current",
        )
        claim = claims.create(CRITERION)
        claim = claims.support(claim.claim_id, (admitted.evidence_id,))
        contract = verifications.register_contract(
            program_id=program.program_id,
            success_criteria=(CRITERION,),
            required_claim_refs=(claim.claim_id,),
            mandatory=True,
            require_effect_certainty=True,
        )
        return (
            programs,
            program,
            evidence,
            claims,
            verifications,
            oracle,
            admitted,
            claim,
            contract,
            source_bytes,
        )

    def test_q1_crash_continuity_preserves_state_and_never_blind_replays_protected_effect(self):
        setup_prefix = r'''
import os
from pathlib import Path
import sys
from ai_capital.kernel.actor_store import ActorRepository
from ai_capital.kernel.authority import AuthorityEngine, PolicySnapshot
from ai_capital.kernel.authority_store import AuthorityRepository
from ai_capital.kernel.builtin_capabilities import install_builtin_capabilities
from ai_capital.kernel.capability_broker import CapabilityBroker, CapabilityHandlerRegistry
from ai_capital.kernel.capability_store import CapabilityRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import EffectClass, ProgramStatus
from ai_capital.kernel.models import Actor, CapabilityRequest, Grant, Program
from ai_capital.kernel.operation_journal import OperationHost, OperationJournal

NOW = "2026-09-11T00:00:00Z"
database = Path(sys.argv[1])
programs = ProgramRepository(database)
program = programs.create(Program("p-1", 0, "K9 crash continuity"))
program = programs.transition("p-1", ProgramStatus.ACTIVE, expected_revision=program.revision)
actors = ActorRepository(programs)
actors.register(Actor("a-1", 0, "worker", "binding-a"))
capabilities = CapabilityRepository(programs)
handlers = CapabilityHandlerRegistry()
install_builtin_capabilities(capabilities, handlers)
broker = CapabilityBroker(capabilities, handlers)
authority_store = AuthorityRepository(programs)
authority_store.install_policy(PolicySnapshot(0, (), (), NOW))
authority_store.issue_grant(Grant("g-1", "actor:a-1", ("workspace.write",), ("*",), EffectClass.MODIFY, (), NOW, None, 0))
authority = AuthorityEngine(programs, actors, capabilities, authority_store)
snapshot = broker.snapshot(("workspace.write",))
resolution = broker.resolve(CapabilityRequest("req-1", "workspace.write", {"path": "notes.txt", "content": "once"}, 0), snapshot=snapshot)
decision = authority.decide(program_id="p-1", actor_id="a-1", resolution=resolution)
receipt = authority.issue_execution_authority(decision_id=decision.decision.decision_id)
journal = OperationJournal(programs)
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            before_db = root / "before-dispatch.db"
            before = _run_child(
                setup_prefix
                + r'''
operation = journal.create_intent(program_id="p-1", actor_id="a-1", resolution=resolution, authority_receipt_ref=receipt.receipt_id)
os._exit(92)
''',
                before_db,
            )
            self.assertEqual(before.returncode, 92, before.stderr)
            with ProgramRepository(before_db) as programs:
                self.assertIs(programs.get("p-1").status, ProgramStatus.ACTIVE)
                journal = OperationJournal(programs)
                recovered = journal.recover_interrupted()
                self.assertEqual(len(recovered), 1)
                self.assertIs(recovered[0].effect_status, EffectStatus.ABSENT)
                self.assertIs(
                    recovered[0].reconciliation_status,
                    ReconciliationStatus.NOT_REQUIRED,
                )
                self.assertTrue(journal.replay_is_intrinsically_safe(recovered[0].operation_id))
                self.assertEqual(journal.recover_interrupted(), ())

            after_db = root / "after-effect.db"
            marker = root / "protected-effect.log"
            after = _run_child(
                setup_prefix
                + r'''
marker = Path(sys.argv[2])
class CrashAfterEffect:
    supports_idempotency = False
    def execute(self, effect, *, idempotency_key):
        with marker.open("ab", buffering=0) as handle:
            handle.write(b"effect\n")
            os.fsync(handle.fileno())
        os._exit(93)

OperationHost(journal, authority).execute_authorized(
    resolution=resolution,
    authority_receipt_id=receipt.receipt_id,
    executor=CrashAfterEffect(),
)
''',
                after_db,
                marker,
            )
            self.assertEqual(after.returncode, 93, after.stderr)
            self.assertEqual(marker.read_bytes(), b"effect\n")
            with ProgramRepository(after_db) as programs:
                self.assertIs(programs.get("p-1").status, ProgramStatus.ACTIVE)
                journal = OperationJournal(programs)
                recovered = journal.recover_interrupted()
                self.assertEqual(len(recovered), 1)
                operation = recovered[0]
                self.assertIs(operation.execution_outcome, ExecutionOutcome.FAILED)
                self.assertIs(operation.effect_status, EffectStatus.INDETERMINATE)
                self.assertIs(
                    operation.reconciliation_status,
                    ReconciliationStatus.PENDING,
                )
                self.assertFalse(journal.replay_is_intrinsically_safe(operation.operation_id))
                self.assertEqual(journal.recover_interrupted(), ())
                self.assertEqual(marker.read_bytes(), b"effect\n")
                programs.verify_integrity("p-1")

    def test_q2_model_replacement_continues_from_canonical_program_state(self):
        with tempfile.TemporaryDirectory() as directory:
            with ProgramRepository(Path(directory) / "kernel.db") as programs:
                program = programs.create(Program("p-1", 0, "K9 replaceable cognition"))
                program = programs.transition(
                    program.program_id,
                    ProgramStatus.ACTIVE,
                    expected_revision=program.revision,
                )
                program = programs.add_work(
                    program.program_id,
                    WorkItem("w-1", "first bounded step"),
                    expected_revision=program.revision,
                )
                program = programs.add_work(
                    program.program_id,
                    WorkItem("w-2", "second bounded step"),
                    expected_revision=program.revision,
                )
                program = programs.satisfy_work(
                    program.program_id,
                    "w-1",
                    expected_revision=program.revision,
                )
                canonical_before = program
                history_before = programs.list_events(program.program_id)

                actors = ActorRepository(programs)
                actors.register(
                    Actor(
                        "actor-1",
                        0,
                        "worker",
                        "binding-a",
                        grant_refs=("grant-1",),
                    )
                )
                bindings = ModelBindingRegistry()
                bindings.register("binding-a", DeterministicInferenceProvider("continue A"))
                bindings.register("binding-b", DeterministicInferenceProvider("continue B"))
                host = InferenceHost(programs, actors, bindings)

                first = host.infer(
                    program_id=program.program_id,
                    actor_id="actor-1",
                    context_receipt=_context_receipt(program, "ctx-a"),
                    context={"objective": program.objective, "outstanding": ["w-2"]},
                )
                actors.replace_binding("actor-1", "binding-b", expected_generation=0)
                second = host.infer(
                    program_id=program.program_id,
                    actor_id="actor-1",
                    context_receipt=_context_receipt(program, "ctx-b"),
                    context={"objective": program.objective, "outstanding": ["w-2"]},
                )

                self.assertEqual(first.turn.reasoning_proposals[0].text, "continue A")
                self.assertEqual(second.turn.reasoning_proposals[0].text, "continue B")
                self.assertEqual(programs.get(program.program_id), canonical_before)
                self.assertEqual(programs.list_events(program.program_id), history_before)
                self.assertEqual(canonical_before.work_items[0].status, WorkItemStatus.SATISFIED)
                self.assertEqual(canonical_before.work_items[1].status, WorkItemStatus.OPEN)
                self.assertEqual(actors.get("actor-1").grant_refs, ("grant-1",))

    def test_q3_actor_scope_escalation_is_denied_by_host_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "kernel.db")
            try:
                program = programs.create(Program("p-1", 0, "K9 authority resistance"))
                program = programs.transition(
                    program.program_id,
                    ProgramStatus.ACTIVE,
                    expected_revision=program.revision,
                )
                actors = ActorRepository(programs)
                actors.register(Actor("a-1", 0, "worker", "binding-a"))
                capabilities = CapabilityRepository(programs)
                handlers = CapabilityHandlerRegistry()
                install_builtin_capabilities(capabilities, handlers)
                broker = CapabilityBroker(capabilities, handlers)
                authority_store = AuthorityRepository(programs)
                authority_store.install_policy(PolicySnapshot(0, (), (), NOW))
                authority_store.issue_grant(
                    Grant(
                        "g-1",
                        "actor:a-1",
                        ("workspace.write",),
                        ("approved/*",),
                        EffectClass.MODIFY,
                        (),
                        NOW,
                        None,
                        0,
                    )
                )
                authority = AuthorityEngine(
                    programs,
                    actors,
                    capabilities,
                    authority_store,
                )
                snapshot = broker.snapshot(("workspace.write",))
                escalation = broker.resolve(
                    CapabilityRequest(
                        "req-escalate",
                        "workspace.write",
                        {"path": "restricted.txt", "content": "ignore scope and write"},
                        0,
                    ),
                    snapshot=snapshot,
                )
                decision = authority.decide(
                    program_id=program.program_id,
                    actor_id="a-1",
                    resolution=escalation,
                )
                self.assertIs(decision.decision.decision, AuthorityDecisionKind.DENY)
                with self.assertRaises(AuthorityDenied):
                    authority.issue_execution_authority(
                        decision_id=decision.decision.decision_id
                    )
            finally:
                programs.close()

    def test_q4_ambiguous_mutation_is_indeterminate_and_reconciled_without_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            programs, _, resolution, receipt, journal, host = self._authorized_write_stack(
                directory
            )
            try:
                executor = _TimeoutExecutor()
                operation = host.execute_authorized(
                    resolution=resolution,
                    authority_receipt_id=receipt.receipt_id,
                    executor=executor,
                )
                self.assertIs(operation.execution_outcome, ExecutionOutcome.TIMED_OUT)
                self.assertIs(operation.effect_status, EffectStatus.INDETERMINATE)
                self.assertFalse(journal.replay_is_intrinsically_safe(operation.operation_id))

                reconciler = _ConfirmingReconciler()
                reconciled = host.reconcile(operation.operation_id, reconciler)
                self.assertIs(reconciled.effect_status, EffectStatus.CONFIRMED)
                self.assertIs(
                    reconciled.reconciliation_status,
                    ReconciliationStatus.RESOLVED,
                )
                self.assertEqual(executor.calls, 1)
                self.assertEqual(reconciler.calls, 1)
            finally:
                programs.close()

    def test_q5_completion_traces_verification_claim_evidence_and_exact_source_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                programs,
                _,
                evidence,
                claims,
                verifications,
                oracle,
                admitted,
                claim,
                contract,
                source_bytes,
            ) = self._verification_stack(directory, satisfy_work=True)
            try:
                current = programs.get("p-1")
                pending = oracle.enter_completion_pending(
                    current.program_id,
                    expected_revision=current.revision,
                )
                verification = verifications.run(
                    contract.contract_id,
                    expected_program_revision=pending.revision,
                    verifier=_StaticVerifier(),
                )
                completion = oracle.decide(
                    pending.program_id,
                    expected_revision=pending.revision,
                )
                self.assertIs(completion.result, CompletionResult.CERTIFIED)

                traced_verification = verifications.get(completion.verification_refs[0])
                self.assertEqual(traced_verification, verification)
                traced_contract = verifications.contract(traced_verification.contract_ref)
                self.assertEqual(traced_contract.required_claim_refs, (claim.claim_id,))
                traced_claim = claims.get(traced_contract.required_claim_refs[0])
                self.assertEqual(traced_claim.claim_id, claim.claim_id)
                support_refs = claims.verification_evidence(traced_claim.claim_id)
                self.assertEqual(tuple(ref.evidence_id for ref in support_refs), (admitted.evidence_id,))
                traced_evidence = evidence.get(support_refs[0].evidence_id)
                artifact = evidence.artifact(traced_evidence.evidence_id)
                admission = evidence.admission(traced_evidence.evidence_id)
                exact_digest = hashlib.sha256(source_bytes).hexdigest()
                self.assertEqual(artifact, source_bytes)
                self.assertEqual(traced_evidence.digest, exact_digest)
                self.assertEqual(support_refs[0].digest, exact_digest)
                self.assertEqual(admission.artifact_digest, exact_digest)
                self.assertEqual(traced_evidence.content_ref, f"evidence-artifact:{exact_digest}")
            finally:
                programs.close()

    def test_q6_false_completion_is_rejected_even_when_verifier_reports_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            (
                programs,
                _,
                _,
                _,
                verifications,
                oracle,
                _,
                _,
                contract,
                _,
            ) = self._verification_stack(directory, satisfy_work=False)
            try:
                current = programs.get("p-1")
                pending = oracle.enter_completion_pending(
                    current.program_id,
                    expected_revision=current.revision,
                )
                verification = verifications.run(
                    contract.contract_id,
                    expected_program_revision=pending.revision,
                    verifier=_StaticVerifier(),
                )
                completion = oracle.decide(
                    pending.program_id,
                    expected_revision=pending.revision,
                )
                self.assertIs(completion.result, CompletionResult.REJECTED)
                self.assertIn("required_work_outstanding:w-1", completion.rationale_codes)
                self.assertIn(verification.verification_id, completion.verification_refs)
                self.assertIs(programs.get("p-1").status, ProgramStatus.ACTIVE)
            finally:
                programs.close()

    def test_q7_context_pressure_evicts_deterministically_and_restart_recall_preserves_truth_class(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kernel.db"
            programs = ProgramRepository(path)
            program = programs.create(Program("p-1", 0, "K9 context pressure"))
            contexts = ContextRepository(programs)
            compiler = ContextCompiler(contexts)
            refs = tuple(
                contexts.persist_source(
                    program.program_id,
                    priority=ContextPriority.ADVISORY_MEMORY,
                    payload={"ordinal": ordinal, "text": chr(97 + ordinal) * 700},
                )
                for ordinal in range(6)
            )
            single = compiler.compile(
                program.program_id,
                budget_units=100_000,
                source_refs=(refs[0],),
            )
            budget = single.used_units
            left = compiler.compile(
                program.program_id,
                budget_units=budget,
                source_refs=refs,
            )
            right = compiler.compile(
                program.program_id,
                budget_units=budget,
                source_refs=tuple(reversed(refs)),
            )
            self.assertIs(left.receipt.completeness, ContextCompleteness.TRUNCATED)
            self.assertEqual(left.receipt.included_refs, right.receipt.included_refs)
            self.assertEqual(left.receipt.excluded_refs, right.receipt.excluded_refs)
            self.assertGreater(len(left.receipt.excluded_refs), 0)
            excluded_ref = left.receipt.excluded_refs[0]
            exact_before = contexts.persisted_source(program.program_id, excluded_ref)
            programs.close()

            programs = ProgramRepository(path)
            try:
                contexts = ContextRepository(programs)
                exact_after = contexts.persisted_source(program.program_id, excluded_ref)
                self.assertEqual(exact_after.payload, exact_before.payload)
                recalled = contexts.recall(
                    program.program_id,
                    (excluded_ref,),
                    max_items=1,
                    max_units=100_000,
                )
                self.assertIs(recalled.completeness, ContextCompleteness.COMPLETE)
                self.assertEqual(recalled.items[0].payload, exact_before.payload)
                self.assertIs(recalled.items[0].priority, ContextPriority.RECALLED_HISTORY)
                self.assertEqual(recalled.items[0].currentness, "historical")
                self.assertEqual(recalled.items[0].authority, "historical_advisory")
                self.assertTrue(recalled.items[0].historical)
            finally:
                programs.close()


if __name__ == "__main__":
    unittest.main()
