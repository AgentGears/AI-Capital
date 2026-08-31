from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_capital.kernel.actor_store import ActorRepository
from ai_capital.kernel.authority import AuthorityEngine, PolicySnapshot
from ai_capital.kernel.authority_store import AuthorityRepository
from ai_capital.kernel.builtin_capabilities import install_builtin_capabilities
from ai_capital.kernel.capability_broker import CapabilityBroker, CapabilityHandlerRegistry
from ai_capital.kernel.capability_store import CapabilityRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import (
    EffectClass,
    EffectStatus,
    ExecutionOutcome,
    ProgramStatus,
    ReconciliationStatus,
)
from ai_capital.kernel.errors import ExecutionTimeout, InvalidRequest
from ai_capital.kernel.models import Actor, CapabilityRequest, Grant, Program
from ai_capital.kernel.operation_journal import (
    ExecutionObservation,
    OperationHost,
    OperationJournal,
    ReconciliationObservation,
)


NOW = "2026-08-30T00:00:00Z"


class ScriptedExecutor:
    def __init__(
        self,
        observation: ExecutionObservation | None = None,
        *,
        error: Exception | None = None,
        supports_idempotency: bool = False,
    ):
        self.observation = observation
        self.error = error
        self.supports_idempotency = supports_idempotency
        self.calls = 0
        self.keys: list[str | None] = []

    def execute(self, effect, *, idempotency_key):
        self.calls += 1
        self.keys.append(idempotency_key)
        if self.error is not None:
            raise self.error
        assert self.observation is not None
        return self.observation


class ScriptedReconciler:
    def __init__(self, observation: ReconciliationObservation):
        self.observation = observation
        self.calls = 0
        self.keys: list[str | None] = []

    def reconcile(self, effect, *, execution_receipt, idempotency_key):
        self.calls += 1
        self.keys.append(idempotency_key)
        return self.observation


class Fixture:
    def __init__(self, directory: str, *, capability_id: str = "workspace.write"):
        self.path = Path(directory) / "kernel.db"
        self.programs = ProgramRepository(self.path)
        self.programs.create(Program("p-1", 0, "operation proof"))
        self.programs.transition("p-1", ProgramStatus.ACTIVE, expected_revision=0)

        self.actors = ActorRepository(self.programs)
        self.actors.register(Actor("a-1", 0, "worker", "binding-a"))

        self.capabilities = CapabilityRepository(self.programs)
        self.handlers = CapabilityHandlerRegistry()
        install_builtin_capabilities(self.capabilities, self.handlers)
        self.broker = CapabilityBroker(self.capabilities, self.handlers)

        self.authority_store = AuthorityRepository(self.programs)
        self.authority_store.install_policy(PolicySnapshot(0, (), (), NOW))
        effect_ceiling = (
            EffectClass.MODIFY
            if capability_id == "workspace.write"
            else EffectClass.OBSERVE
        )
        self.authority_store.issue_grant(
            Grant(
                "g-1",
                "actor:a-1",
                (capability_id,),
                ("*",),
                effect_ceiling,
                (),
                NOW,
                None,
                0,
            )
        )
        self.authority = AuthorityEngine(
            self.programs,
            self.actors,
            self.capabilities,
            self.authority_store,
        )
        self.journal = OperationJournal(self.programs)
        self.host = OperationHost(self.journal, self.authority)
        self.capability_id = capability_id

    def resolution(self, request_id: str):
        snapshot = self.broker.snapshot((self.capability_id,))
        arguments = (
            {"path": "notes.txt", "content": "updated"}
            if self.capability_id == "workspace.write"
            else {"path": "notes.txt"}
        )
        return self.broker.resolve(
            CapabilityRequest(request_id, self.capability_id, arguments, 0),
            snapshot=snapshot,
        )

    def authorize(self, request_id: str = "req-1"):
        resolution = self.resolution(request_id)
        decision = self.authority.decide(
            program_id="p-1",
            actor_id="a-1",
            resolution=resolution,
        )
        receipt = self.authority.issue_execution_authority(
            decision_id=decision.decision.decision_id
        )
        return resolution, receipt

    def close(self):
        self.programs.close()


class K5OperationJournalTests(unittest.TestCase):
    def test_success_with_confirmed_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = Fixture(directory)
            try:
                resolution, authority = fx.authorize()
                executor = ScriptedExecutor(
                    ExecutionObservation(
                        ExecutionOutcome.SUCCEEDED,
                        EffectStatus.CONFIRMED,
                        {"bytes_written": 7},
                        backend_receipt_ref="backend-1",
                    )
                )
                operation = fx.host.execute_authorized(
                    resolution=resolution,
                    authority_receipt_id=authority.receipt_id,
                    executor=executor,
                )
                self.assertIs(operation.execution_outcome, ExecutionOutcome.SUCCEEDED)
                self.assertIs(operation.effect_status, EffectStatus.CONFIRMED)
                self.assertIs(
                    operation.reconciliation_status,
                    ReconciliationStatus.NOT_REQUIRED,
                )
                self.assertEqual(executor.calls, 1)
            finally:
                fx.close()

    def test_explicit_failure_with_absent_effect_is_retry_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = Fixture(directory)
            try:
                resolution, authority = fx.authorize()
                executor = ScriptedExecutor(
                    ExecutionObservation(
                        ExecutionOutcome.FAILED,
                        EffectStatus.ABSENT,
                        {},
                        error_code="rejected_before_effect",
                    )
                )
                operation = fx.host.execute_authorized(
                    resolution=resolution,
                    authority_receipt_id=authority.receipt_id,
                    executor=executor,
                )
                self.assertIs(operation.execution_outcome, ExecutionOutcome.FAILED)
                self.assertIs(operation.effect_status, EffectStatus.ABSENT)
                self.assertTrue(
                    fx.journal.replay_is_intrinsically_safe(operation.operation_id)
                )
            finally:
                fx.close()

    def test_timeout_then_reconciliation_confirms_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = Fixture(directory)
            try:
                resolution, authority = fx.authorize()
                executor = ScriptedExecutor(error=ExecutionTimeout("lost acknowledgement"))
                operation = fx.host.execute_authorized(
                    resolution=resolution,
                    authority_receipt_id=authority.receipt_id,
                    executor=executor,
                )
                self.assertIs(operation.execution_outcome, ExecutionOutcome.TIMED_OUT)
                self.assertIs(operation.effect_status, EffectStatus.INDETERMINATE)
                self.assertFalse(
                    fx.journal.replay_is_intrinsically_safe(operation.operation_id)
                )

                reconciler = ScriptedReconciler(
                    ReconciliationObservation(
                        EffectStatus.CONFIRMED,
                        "effect_observed",
                        ("evidence:later",),
                    )
                )
                reconciled = fx.host.reconcile(operation.operation_id, reconciler)
                self.assertIs(reconciled.effect_status, EffectStatus.CONFIRMED)
                self.assertIs(
                    reconciled.reconciliation_status,
                    ReconciliationStatus.RESOLVED,
                )
                self.assertEqual(executor.calls, 1)
            finally:
                fx.close()

    def test_timeout_then_reconciliation_proves_absence(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = Fixture(directory)
            try:
                resolution, authority = fx.authorize()
                operation = fx.host.execute_authorized(
                    resolution=resolution,
                    authority_receipt_id=authority.receipt_id,
                    executor=ScriptedExecutor(
                        error=ExecutionTimeout("lost acknowledgement")
                    ),
                )
                reconciled = fx.host.reconcile(
                    operation.operation_id,
                    ScriptedReconciler(
                        ReconciliationObservation(
                            EffectStatus.ABSENT,
                            "effect_absent",
                        )
                    ),
                )
                self.assertIs(reconciled.effect_status, EffectStatus.ABSENT)
                self.assertIs(
                    reconciled.reconciliation_status,
                    ReconciliationStatus.RESOLVED,
                )
                self.assertTrue(
                    fx.journal.replay_is_intrinsically_safe(reconciled.operation_id)
                )
            finally:
                fx.close()

    def test_timeout_can_remain_permanently_indeterminate(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = Fixture(directory)
            try:
                resolution, authority = fx.authorize()
                executor = ScriptedExecutor(
                    error=ExecutionTimeout("lost acknowledgement"),
                    supports_idempotency=True,
                )
                operation = fx.host.execute_authorized(
                    resolution=resolution,
                    authority_receipt_id=authority.receipt_id,
                    executor=executor,
                    idempotency_key="caller-opt-in",
                )
                host_key = fx.journal.idempotency_key(operation.operation_id)
                self.assertIsNotNone(host_key)
                self.assertNotEqual(host_key, "caller-opt-in")
                reconciler = ScriptedReconciler(
                    ReconciliationObservation(
                        EffectStatus.INDETERMINATE,
                        "cannot_observe_effect",
                    )
                )
                reconciled = fx.host.reconcile(operation.operation_id, reconciler)
                self.assertIs(reconciled.effect_status, EffectStatus.INDETERMINATE)
                self.assertIs(
                    reconciled.reconciliation_status,
                    ReconciliationStatus.UNRESOLVED,
                )
                self.assertFalse(
                    fx.journal.replay_is_intrinsically_safe(reconciled.operation_id)
                )
                self.assertEqual(executor.keys, [host_key])
                self.assertEqual(reconciler.keys, [host_key])
                self.assertEqual(executor.calls, 1)
            finally:
                fx.close()

    def test_process_restart_after_dispatch_boundary_becomes_indeterminate(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = Fixture(directory)
            try:
                resolution, authority = fx.authorize()
                operation = fx.journal.create_intent(
                    program_id="p-1",
                    actor_id="a-1",
                    resolution=resolution,
                    authority_receipt_ref=authority.receipt_id,
                )
                fx.authority.consume_execution_authority(
                    receipt_id=authority.receipt_id
                )
                fx.journal.mark_admitted(operation.operation_id)
                fx.journal.mark_running(operation.operation_id)
                operation_id = operation.operation_id
            finally:
                fx.close()

            with ProgramRepository(Path(directory) / "kernel.db") as programs:
                recovered_journal = OperationJournal(programs)
                recovered = recovered_journal.recover_interrupted()
                self.assertEqual(len(recovered), 1)
                operation = recovered[0]
                self.assertEqual(operation.operation_id, operation_id)
                self.assertIs(operation.execution_outcome, ExecutionOutcome.FAILED)
                self.assertIs(operation.effect_status, EffectStatus.INDETERMINATE)
                self.assertIs(
                    operation.reconciliation_status,
                    ReconciliationStatus.PENDING,
                )
                self.assertFalse(
                    recovered_journal.replay_is_intrinsically_safe(operation_id)
                )

    def test_restart_before_dispatch_proves_mutation_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = Fixture(directory)
            try:
                resolution, authority = fx.authorize()
                operation = fx.journal.create_intent(
                    program_id="p-1",
                    actor_id="a-1",
                    resolution=resolution,
                    authority_receipt_ref=authority.receipt_id,
                )
                operation_id = operation.operation_id
            finally:
                fx.close()

            with ProgramRepository(Path(directory) / "kernel.db") as programs:
                recovered_journal = OperationJournal(programs)
                recovered = recovered_journal.recover_interrupted()
                self.assertEqual(len(recovered), 1)
                operation = recovered[0]
                self.assertEqual(operation.operation_id, operation_id)
                self.assertIs(operation.execution_outcome, ExecutionOutcome.FAILED)
                self.assertIs(operation.effect_status, EffectStatus.ABSENT)
                self.assertIs(
                    operation.reconciliation_status,
                    ReconciliationStatus.NOT_REQUIRED,
                )
                self.assertTrue(
                    recovered_journal.replay_is_intrinsically_safe(operation_id)
                )

    def test_idempotency_key_requires_backend_support(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = Fixture(directory)
            try:
                resolution, authority = fx.authorize()
                with self.assertRaises(InvalidRequest):
                    fx.host.execute_authorized(
                        resolution=resolution,
                        authority_receipt_id=authority.receipt_id,
                        executor=ScriptedExecutor(
                            ExecutionObservation(
                                ExecutionOutcome.SUCCEEDED,
                                EffectStatus.CONFIRMED,
                                {},
                            )
                        ),
                        idempotency_key="caller-opt-in",
                    )
            finally:
                fx.close()

    def test_observation_failure_has_no_environmental_effect_dimension(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = Fixture(directory, capability_id="workspace.read")
            try:
                resolution, authority = fx.authorize()
                operation = fx.host.execute_authorized(
                    resolution=resolution,
                    authority_receipt_id=authority.receipt_id,
                    executor=ScriptedExecutor(error=ExecutionTimeout("read timed out")),
                )
                self.assertIs(operation.execution_outcome, ExecutionOutcome.TIMED_OUT)
                self.assertIs(operation.effect_status, EffectStatus.NOT_APPLICABLE)
                self.assertIs(
                    operation.reconciliation_status,
                    ReconciliationStatus.NOT_REQUIRED,
                )
            finally:
                fx.close()


if __name__ == "__main__":
    unittest.main()
