from __future__ import annotations

from dataclasses import dataclass, replace
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
from ai_capital.kernel.durable_program import ProgramRepository, _validate_event_semantics
from ai_capital.kernel.enums import (
    EffectClass,
    ModelAttemptOutcome,
    ProgramStatus,
    Reversibility,
    RiskClass,
    WorkItemStatus,
)
from ai_capital.kernel.errors import (
    IntegrityViolation,
    InvalidStateTransition,
    PersistenceConflict,
)
from ai_capital.kernel.inference import _validate_model_turn
from ai_capital.kernel.models import (
    Actor,
    Capability,
    CapabilityRequest,
    CapabilityResolution,
    ContextReceipt,
    Grant,
    InferenceRequest,
    ModelAttemptReceipt,
    ModelTurn,
    Program,
    ReasoningProposal,
    ResolvedEffect,
    WorkItem,
)
from ai_capital.kernel.operation_journal import OperationJournal
from ai_capital.kernel.schema_codec import record_from_json, record_to_json
from ai_capital.kernel.serialization import canonical_digest


NOW = "2026-08-30T00:00:00Z"


@dataclass(frozen=True, slots=True)
class FloatRecord:
    value: float


class InvalidTargetResolver:
    def resolve_effect(self, arguments):
        return ResolvedEffect(
            resource_type="workspace_path",
            target=None,
            effect_class=EffectClass.OBSERVE,
            parameters={},
        )


class ReviewRemediationTests(unittest.TestCase):
    def test_float_overflow_is_rejected_during_decode(self):
        with self.assertRaises(ValueError):
            record_from_json(FloatRecord, '{"value":1e400}')

    def test_program_revised_event_cannot_change_work_state(self):
        previous = Program(
            "p-1",
            0,
            "objective",
            work_items=(WorkItem("w-1", "work"),),
        )
        candidate = replace(
            previous,
            revision=1,
            work_items=(WorkItem("w-1", "work", WorkItemStatus.SATISFIED),),
        )
        with self.assertRaises(IntegrityViolation):
            _validate_event_semantics(previous, candidate, "program.revised")

        allowed = replace(previous, revision=1, objective="revised objective")
        _validate_event_semantics(previous, allowed, "program.revised")

    def test_repository_preserves_invalid_state_transition_error(self):
        with tempfile.TemporaryDirectory() as directory:
            with ProgramRepository(Path(directory) / "kernel.db") as programs:
                programs.create(Program("p-1", 0, "objective"))
                with self.assertRaises(InvalidStateTransition):
                    programs.transition(
                        "p-1",
                        ProgramStatus.COMPLETED,
                        expected_revision=0,
                    )

    def test_model_turn_nested_schema_is_validated_before_acceptance(self):
        malformed = ModelTurn(
            provenance_receipt="attempt-1",
            reasoning_proposals=("not-a-reasoning-proposal",),
        )
        with self.assertRaises(IntegrityViolation):
            _validate_model_turn(malformed, attempt_id="attempt-1")

    def test_loaded_model_turn_is_bound_to_attempt_receipt_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            with ProgramRepository(Path(directory) / "kernel.db") as programs:
                actors = ActorRepository(programs)
                actors.register(Actor("a-1", 0, "worker", "binding-a"))
                context_receipt = ContextReceipt(
                    "ctx-1",
                    "p-1",
                    0,
                    (),
                    (),
                    "complete",
                    10,
                    NOW,
                )
                request = InferenceRequest(
                    "attempt-1",
                    "a-1",
                    0,
                    "p-1",
                    0,
                    "binding-a",
                    context_receipt,
                    {},
                )
                turn = ModelTurn(
                    provenance_receipt="attempt-1",
                    reasoning_proposals=(ReasoningProposal("original"),),
                )
                receipt = ModelAttemptReceipt(
                    "attempt-1",
                    "a-1",
                    0,
                    "p-1",
                    0,
                    "binding-a",
                    "ctx-1",
                    canonical_digest(request),
                    "config-digest",
                    ModelAttemptOutcome.SUCCEEDED,
                    NOW,
                    NOW,
                    canonical_digest(turn),
                    None,
                )
                actors.record_attempt(receipt, turn, request)

                forged = replace(
                    turn,
                    reasoning_proposals=(ReasoningProposal("forged"),),
                )
                programs._db.execute(
                    """
                    UPDATE model_attempts
                    SET turn_json = ?, turn_digest = ?
                    WHERE attempt_id = ?
                    """,
                    (
                        record_to_json(forged),
                        canonical_digest(forged),
                        "attempt-1",
                    ),
                )
                with self.assertRaises(IntegrityViolation):
                    actors.turn("attempt-1")

    def test_command_observe_rejects_mutating_command(self):
        with tempfile.TemporaryDirectory() as directory:
            with ProgramRepository(Path(directory) / "kernel.db") as programs:
                capabilities = CapabilityRepository(programs)
                handlers = CapabilityHandlerRegistry()
                install_builtin_capabilities(capabilities, handlers)
                broker = CapabilityBroker(capabilities, handlers)
                snapshot = broker.snapshot(("command.observe",))
                with self.assertRaises(IntegrityViolation):
                    broker.resolve(
                        CapabilityRequest(
                            "req-1",
                            "command.observe",
                            {"command": "rm -rf important"},
                            0,
                        ),
                        snapshot=snapshot,
                    )

    def test_builtin_installation_is_restart_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kernel.db"
            with ProgramRepository(path) as programs:
                capabilities = CapabilityRepository(programs)
                handlers = CapabilityHandlerRegistry()
                install_builtin_capabilities(capabilities, handlers)
                self.assertTrue(handlers.contains("builtin.workspace.read.v1"))

            with ProgramRepository(path) as programs:
                capabilities = CapabilityRepository(programs)
                handlers = CapabilityHandlerRegistry()
                install_builtin_capabilities(capabilities, handlers)
                self.assertTrue(handlers.contains("builtin.workspace.read.v1"))
                self.assertEqual(capabilities.get("workspace.read").binding_revision, 0)

    def test_broker_contains_non_string_resolver_target(self):
        with tempfile.TemporaryDirectory() as directory:
            with ProgramRepository(Path(directory) / "kernel.db") as programs:
                capabilities = CapabilityRepository(programs)
                handlers = CapabilityHandlerRegistry()
                capability = Capability(
                    "bad.target",
                    1,
                    "read",
                    "workspace_path",
                    EffectClass.OBSERVE,
                    Reversibility.REVERSIBLE,
                    RiskClass.LOW,
                    {
                        "type": "object",
                        "properties": {},
                        "required": (),
                        "additional_properties": False,
                    },
                    {
                        "type": "object",
                        "properties": {},
                        "required": (),
                        "additional_properties": True,
                    },
                    0,
                    "bad.target.v1",
                )
                capabilities.register(capability)
                handlers.register("bad.target.v1", InvalidTargetResolver())
                broker = CapabilityBroker(capabilities, handlers)
                snapshot = broker.snapshot(("bad.target",))
                with self.assertRaises(IntegrityViolation):
                    broker.resolve(
                        CapabilityRequest("req-1", "bad.target", {}, 0),
                        snapshot=snapshot,
                    )

    def test_execution_authority_receipt_is_bound_to_stored_decision_context(self):
        with tempfile.TemporaryDirectory() as directory:
            with ProgramRepository(Path(directory) / "kernel.db") as programs:
                programs.create(Program("p-1", 0, "authority"))
                programs.transition("p-1", ProgramStatus.ACTIVE, expected_revision=0)
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
                authority = AuthorityEngine(
                    programs,
                    actors,
                    capabilities,
                    authority_store,
                )
                snapshot = broker.snapshot(("workspace.write",))
                resolution = broker.resolve(
                    CapabilityRequest(
                        "req-1",
                        "workspace.write",
                        {"path": "notes.txt", "content": "x"},
                        0,
                    ),
                    snapshot=snapshot,
                )
                decision = authority.decide(
                    program_id="p-1",
                    actor_id="a-1",
                    resolution=resolution,
                )
                receipt = authority.issue_execution_authority(
                    decision_id=decision.decision.decision_id
                )
                forged = replace(receipt, program_id="p-forged")
                programs._db.execute(
                    """
                    UPDATE execution_authority_receipts
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
                    authority_store.get_execution_authority(receipt.receipt_id)

    @staticmethod
    def _resolution(request_id: str, target: str) -> CapabilityResolution:
        return CapabilityResolution(
            request_id,
            "workspace.write",
            0,
            {"path": target, "content": "x"},
            ResolvedEffect(
                "workspace_path",
                target,
                EffectClass.MODIFY,
                {"content": "x"},
            ),
        )

    def test_idempotency_identity_cannot_be_reused_across_operations(self):
        with tempfile.TemporaryDirectory() as directory:
            with ProgramRepository(Path(directory) / "kernel.db") as programs:
                journal = OperationJournal(programs)
                journal.create_intent(
                    program_id="p-1",
                    actor_id="a-1",
                    resolution=self._resolution("req-1", "a.txt"),
                    authority_receipt_ref="auth-1",
                    idempotency_key="idem-1",
                )
                with self.assertRaises(PersistenceConflict):
                    journal.create_intent(
                        program_id="p-1",
                        actor_id="a-1",
                        resolution=self._resolution("req-2", "b.txt"),
                        authority_receipt_ref="auth-2",
                        idempotency_key="idem-1",
                    )

    def test_persisted_idempotency_identity_is_integrity_protected(self):
        with tempfile.TemporaryDirectory() as directory:
            with ProgramRepository(Path(directory) / "kernel.db") as programs:
                journal = OperationJournal(programs)
                operation = journal.create_intent(
                    program_id="p-1",
                    actor_id="a-1",
                    resolution=self._resolution("req-1", "a.txt"),
                    authority_receipt_ref="auth-1",
                    idempotency_key="idem-1",
                )
                programs._db.execute(
                    """
                    UPDATE operation_projections
                    SET idempotency_key = 'tampered'
                    WHERE operation_id = ?
                    """,
                    (operation.operation_id,),
                )
                with self.assertRaises(IntegrityViolation):
                    journal.idempotency_key(operation.operation_id)


if __name__ == "__main__":
    unittest.main()
