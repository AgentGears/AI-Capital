from dataclasses import replace
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
from ai_capital.kernel.errors import (
    ExecutionCancelled,
    ExecutionTimeout,
    IntegrityViolation,
)
from ai_capital.kernel.models import Actor, CapabilityRequest, Grant, Program
from ai_capital.kernel.operation_journal import (
    ExecutionObservation,
    OperationHost,
    OperationJournal,
    ReconciliationObservation,
)
from ai_capital.kernel.schema_codec import record_to_json
from ai_capital.kernel.serialization import canonical_digest


NOW = "2026-08-30T00:00:00Z"


class Executor:
    supports_idempotency = False

    def __init__(self, observation=None, error=None):
        self.observation = observation
        self.error = error
        self.calls = 0

    def execute(self, effect, *, idempotency_key):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.observation


class SideEffectThenExitExecutor:
    supports_idempotency = False

    def __init__(self, marker: Path):
        self.marker = marker
        self.calls = 0

    def execute(self, effect, *, idempotency_key):
        self.calls += 1
        self.marker.write_text("effect happened", encoding="utf-8")
        raise SystemExit("acknowledgement lost after effect")


class SideEffectThenCancelExecutor:
    supports_idempotency = False

    def __init__(self, marker: Path):
        self.marker = marker
        self.calls = 0

    def execute(self, effect, *, idempotency_key):
        self.calls += 1
        self.marker.write_text("effect happened", encoding="utf-8")
        raise ExecutionCancelled("cancelled after effect")


class DuplicateDeliveryExecutor:
    supports_idempotency = True

    def __init__(self):
        self.calls = 0
        self.deliveries = 0
        self.effects = 0
        self._seen: set[str] = set()

    def _deliver(self, key: str) -> None:
        self.deliveries += 1
        if key in self._seen:
            return
        self._seen.add(key)
        self.effects += 1

    def execute(self, effect, *, idempotency_key):
        self.calls += 1
        assert idempotency_key is not None
        self._deliver(idempotency_key)
        self._deliver(idempotency_key)
        return ExecutionObservation(
            ExecutionOutcome.SUCCEEDED,
            EffectStatus.CONFIRMED,
            {"deduplicated": True},
        )


class Fixture:
    def __init__(self, directory: str, *, capability_id: str = "workspace.write"):
        self.path = Path(directory) / "kernel.db"
        self.programs = ProgramRepository(self.path)
        self.programs.create(Program("p-1", 0, "operation integrity"))
        self.programs.transition("p-1", ProgramStatus.ACTIVE, expected_revision=0)
        self.actors = ActorRepository(self.programs)
        self.actors.register(Actor("a-1", 0, "worker", "binding-a"))
        self.capabilities = CapabilityRepository(self.programs)
        self.handlers = CapabilityHandlerRegistry()
        install_builtin_capabilities(self.capabilities, self.handlers)
        self.broker = CapabilityBroker(self.capabilities, self.handlers)
        self.authority_store = AuthorityRepository(self.programs)
        self.authority_store.install_policy(PolicySnapshot(0, (), (), NOW))
        ceiling = (
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
                ceiling,
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

    def authorize(self, request_id="req-1"):
        snapshot = self.broker.snapshot((self.capability_id,))
        arguments = (
            {"path": "notes.txt", "content": "updated"}
            if self.capability_id == "workspace.write"
            else {"path": "notes.txt"}
        )
        resolution = self.broker.resolve(
            CapabilityRequest(request_id, self.capability_id, arguments, 0),
            snapshot=snapshot,
        )
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


class K5OperationIntegrityTests(unittest.TestCase):
    def test_lost_ack_after_real_side_effect_recovers_indeterminate_without_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "effect.marker"
            fx = Fixture(directory)
            try:
                resolution, authority = fx.authorize()
                executor = SideEffectThenExitExecutor(marker)
                with self.assertRaises(SystemExit):
                    fx.host.execute_authorized(
                        resolution=resolution,
                        authority_receipt_id=authority.receipt_id,
                        executor=executor,
                    )
                self.assertEqual(executor.calls, 1)
                self.assertEqual(marker.read_text(encoding="utf-8"), "effect happened")
                row = fx.programs._db.execute(
                    "SELECT operation_id FROM operation_projections"
                ).fetchone()
                operation_id = row["operation_id"]
                running = fx.journal.get(operation_id)
                self.assertIs(running.execution_outcome, ExecutionOutcome.RUNNING)
            finally:
                fx.close()

            with ProgramRepository(Path(directory) / "kernel.db") as programs:
                journal = OperationJournal(programs)
                recovered = journal.recover_interrupted()
                self.assertEqual(len(recovered), 1)
                operation = recovered[0]
                self.assertEqual(operation.operation_id, operation_id)
                self.assertIs(operation.execution_outcome, ExecutionOutcome.FAILED)
                self.assertIs(operation.effect_status, EffectStatus.INDETERMINATE)
                self.assertIs(
                    operation.reconciliation_status,
                    ReconciliationStatus.PENDING,
                )
                self.assertFalse(journal.replay_is_intrinsically_safe(operation_id))
                self.assertEqual(marker.read_text(encoding="utf-8"), "effect happened")

    def test_cancellation_after_real_side_effect_is_indeterminate(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "cancel.marker"
            fx = Fixture(directory)
            try:
                resolution, authority = fx.authorize()
                executor = SideEffectThenCancelExecutor(marker)
                operation = fx.host.execute_authorized(
                    resolution=resolution,
                    authority_receipt_id=authority.receipt_id,
                    executor=executor,
                )
                self.assertEqual(executor.calls, 1)
                self.assertEqual(marker.read_text(encoding="utf-8"), "effect happened")
                self.assertIs(operation.execution_outcome, ExecutionOutcome.CANCELLED)
                self.assertIs(operation.effect_status, EffectStatus.INDETERMINATE)
                self.assertIs(
                    operation.reconciliation_status,
                    ReconciliationStatus.PENDING,
                )
                self.assertFalse(
                    fx.journal.replay_is_intrinsically_safe(operation.operation_id)
                )
            finally:
                fx.close()

    def test_duplicate_adapter_delivery_uses_same_idempotency_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = Fixture(directory)
            try:
                resolution, authority = fx.authorize()
                executor = DuplicateDeliveryExecutor()
                operation = fx.host.execute_authorized(
                    resolution=resolution,
                    authority_receipt_id=authority.receipt_id,
                    executor=executor,
                    idempotency_key="host-idem-1",
                )
                self.assertEqual(executor.calls, 1)
                self.assertEqual(executor.deliveries, 2)
                self.assertEqual(executor.effects, 1)
                self.assertEqual(
                    fx.journal.idempotency_key(operation.operation_id),
                    "host-idem-1",
                )
                self.assertIs(operation.effect_status, EffectStatus.CONFIRMED)
            finally:
                fx.close()

    def test_semantic_event_sequence_includes_admission(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = Fixture(directory)
            try:
                resolution, authority = fx.authorize()
                fx.host.execute_authorized(
                    resolution=resolution,
                    authority_receipt_id=authority.receipt_id,
                    executor=Executor(
                        ExecutionObservation(
                            ExecutionOutcome.SUCCEEDED,
                            EffectStatus.CONFIRMED,
                            {},
                        )
                    ),
                )
                rows = fx.programs._db.execute(
                    """
                    SELECT event_type FROM events
                    WHERE event_type LIKE 'operation.%'
                    ORDER BY sequence
                    """
                ).fetchall()
                self.assertEqual(
                    [row["event_type"] for row in rows],
                    [
                        "operation.requested",
                        "operation.admitted",
                        "operation.started",
                        "operation.finished",
                    ],
                )
            finally:
                fx.close()

    def test_observe_backend_cannot_claim_environmental_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = Fixture(directory, capability_id="workspace.read")
            try:
                resolution, authority = fx.authorize()
                operation = fx.journal.create_intent(
                    program_id="p-1",
                    actor_id="a-1",
                    resolution=resolution,
                    authority_receipt_ref=authority.receipt_id,
                )
                fx.authority.consume_execution_authority(receipt_id=authority.receipt_id)
                fx.journal.mark_admitted(operation.operation_id)
                fx.journal.mark_running(operation.operation_id)
                with self.assertRaises(IntegrityViolation):
                    fx.journal.finish(
                        operation.operation_id,
                        ExecutionObservation(
                            ExecutionOutcome.SUCCEEDED,
                            EffectStatus.CONFIRMED,
                            {},
                        ),
                    )
                self.assertIs(
                    fx.journal.get(operation.operation_id).execution_outcome,
                    ExecutionOutcome.RUNNING,
                )
            finally:
                fx.close()

    def test_mutation_backend_cannot_use_not_applicable_effect(self):
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
                fx.authority.consume_execution_authority(receipt_id=authority.receipt_id)
                fx.journal.mark_admitted(operation.operation_id)
                fx.journal.mark_running(operation.operation_id)
                with self.assertRaises(IntegrityViolation):
                    fx.journal.finish(
                        operation.operation_id,
                        ExecutionObservation(
                            ExecutionOutcome.FAILED,
                            EffectStatus.NOT_APPLICABLE,
                            {},
                        ),
                    )
            finally:
                fx.close()

    def test_projection_digest_tampering_is_rejected(self):
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
                fx.programs._db.execute(
                    "UPDATE operation_projections SET operation_digest = 'forged' WHERE operation_id = ?",
                    (operation.operation_id,),
                )
                with self.assertRaises(IntegrityViolation):
                    fx.journal.get(operation.operation_id)
            finally:
                fx.close()

    def test_resolution_tampering_with_valid_digest_is_rejected_by_request_binding(self):
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
                forged = replace(resolution, request_id="forged-request")
                fx.programs._db.execute(
                    """
                    UPDATE operation_projections
                    SET resolution_json = ?, resolution_digest = ?
                    WHERE operation_id = ?
                    """,
                    (
                        record_to_json(forged),
                        canonical_digest(forged),
                        operation.operation_id,
                    ),
                )
                with self.assertRaises(IntegrityViolation):
                    fx.journal.resolution(operation.operation_id)
            finally:
                fx.close()

    def test_execution_receipt_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = Fixture(directory)
            try:
                resolution, authority = fx.authorize()
                operation = fx.host.execute_authorized(
                    resolution=resolution,
                    authority_receipt_id=authority.receipt_id,
                    executor=Executor(
                        ExecutionObservation(
                            ExecutionOutcome.SUCCEEDED,
                            EffectStatus.CONFIRMED,
                            {},
                        )
                    ),
                )
                fx.programs._db.execute(
                    """
                    UPDATE operation_receipts SET receipt_digest = 'forged'
                    WHERE operation_id = ? AND receipt_type = 'execution'
                    """,
                    (operation.operation_id,),
                )
                with self.assertRaises(IntegrityViolation):
                    fx.journal.execution_receipt(operation.operation_id)
            finally:
                fx.close()

    def test_empty_reconciliation_rationale_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = Fixture(directory)
            try:
                resolution, authority = fx.authorize()
                operation = fx.host.execute_authorized(
                    resolution=resolution,
                    authority_receipt_id=authority.receipt_id,
                    executor=Executor(error=ExecutionTimeout("lost ack")),
                )
                with self.assertRaises(IntegrityViolation):
                    fx.journal.apply_reconciliation(
                        operation.operation_id,
                        ReconciliationObservation(
                            EffectStatus.CONFIRMED,
                            "",
                        ),
                    )
            finally:
                fx.close()


if __name__ == "__main__":
    unittest.main()
