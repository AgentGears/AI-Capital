from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from ai_capital.kernel.actor_store import ActorRepository
from ai_capital.kernel.authority import AuthorityEngine, PolicySnapshot
from ai_capital.kernel.authority_store import AuthorityRepository
from ai_capital.kernel.builtin_capabilities import install_builtin_capabilities
from ai_capital.kernel.capability_broker import CapabilityBroker, CapabilityHandlerRegistry
from ai_capital.kernel.capability_store import CapabilityRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import (
    AuthorityDecisionKind,
    EffectClass,
    ProgramStatus,
    RiskClass,
)
from ai_capital.kernel.errors import (
    ApprovalConsumed,
    ApprovalInvalid,
    ApprovalRequired,
    AuthorityDenied,
    IntegrityViolation,
    StaleActorGeneration,
    StaleCapabilityBinding,
    StaleProgramRevision,
)
from ai_capital.kernel.models import (
    Actor,
    CapabilityRequest,
    Grant,
    Program,
    ResolvedEffect,
)


NOW = "2026-08-30T00:00:00Z"


class Fixture:
    def __init__(
        self,
        directory: str,
        *,
        grant: bool = True,
        approval_required: bool = False,
        ask_risks: tuple[RiskClass, ...] = (),
        deny_effects: tuple[EffectClass, ...] = (),
        capability_id: str = "workspace.read",
        effect_ceiling: EffectClass = EffectClass.OBSERVE,
        resource_scope: tuple[str, ...] = ("*",),
    ):
        self.path = Path(directory) / "kernel.db"
        self.programs = ProgramRepository(self.path)
        self.program = self.programs.create(Program("p-1", 0, "authority proof"))
        self.actors = ActorRepository(self.programs)
        self.actors.register(Actor("a-1", 0, "worker", "binding-a"))
        self.capabilities = CapabilityRepository(self.programs)
        self.handlers = CapabilityHandlerRegistry()
        install_builtin_capabilities(self.capabilities, self.handlers)
        self.broker = CapabilityBroker(self.capabilities, self.handlers)
        self.store = AuthorityRepository(self.programs)
        self.store.install_policy(
            PolicySnapshot(0, ask_risks, deny_effects, NOW)
        )
        if grant:
            constraints = ("approval_required",) if approval_required else ()
            self.store.issue_grant(
                Grant(
                    "g-1",
                    "actor:a-1",
                    (capability_id,),
                    resource_scope,
                    effect_ceiling,
                    constraints,
                    NOW,
                    None,
                    0,
                )
            )
        self.engine = AuthorityEngine(
            self.programs,
            self.actors,
            self.capabilities,
            self.store,
        )
        snapshot = self.broker.snapshot((capability_id,))
        self.resolution = self.broker.resolve(
            CapabilityRequest(
                "req-1",
                capability_id,
                self._arguments(capability_id),
                0,
            ),
            snapshot=snapshot,
        )

    @staticmethod
    def _arguments(capability_id: str):
        if capability_id == "workspace.read":
            return {"path": "notes.txt"}
        if capability_id == "workspace.write":
            return {"path": "notes.txt", "content": "updated"}
        raise AssertionError(capability_id)

    def close(self):
        self.programs.close()


class K4AuthorityTests(unittest.TestCase):
    def test_matching_grant_and_policy_allow_yields_single_use_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = Fixture(directory)
            try:
                context = fx.engine.decide(
                    program_id="p-1", actor_id="a-1", resolution=fx.resolution
                )
                self.assertIs(context.decision.decision, AuthorityDecisionKind.ALLOW)
                receipt = fx.engine.issue_execution_authority(
                    decision_id=context.decision.decision_id
                )
                self.assertEqual(receipt.grant_refs, ("g-1",))
                fx.engine.consume_execution_authority(receipt_id=receipt.receipt_id)
                with self.assertRaises(AuthorityDenied):
                    fx.engine.consume_execution_authority(receipt_id=receipt.receipt_id)
            finally:
                fx.close()

    def test_capability_availability_without_grant_denies(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = Fixture(directory, grant=False)
            try:
                context = fx.engine.decide(
                    program_id="p-1", actor_id="a-1", resolution=fx.resolution
                )
                self.assertIs(context.decision.decision, AuthorityDecisionKind.DENY)
                self.assertEqual(context.decision.rationale_code, "no_applicable_grant")
                with self.assertRaises(AuthorityDenied):
                    fx.engine.issue_execution_authority(
                        decision_id=context.decision.decision_id
                    )
            finally:
                fx.close()

    def test_ask_requires_one_shot_effect_bound_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = Fixture(directory, approval_required=True)
            try:
                context = fx.engine.decide(
                    program_id="p-1", actor_id="a-1", resolution=fx.resolution
                )
                self.assertIs(context.decision.decision, AuthorityDecisionKind.ASK)
                with self.assertRaises(ApprovalRequired):
                    fx.engine.issue_execution_authority(
                        decision_id=context.decision.decision_id
                    )
                approval = fx.engine.approve(decision_id=context.decision.decision_id)
                receipt = fx.engine.issue_execution_authority(
                    decision_id=context.decision.decision_id,
                    approval_id=approval.approval_id,
                )
                self.assertEqual(receipt.decision_id, context.decision.decision_id)
                with self.assertRaises(ApprovalConsumed):
                    fx.engine.issue_execution_authority(
                        decision_id=context.decision.decision_id,
                        approval_id=approval.approval_id,
                    )
            finally:
                fx.close()

    def test_approval_cannot_authorize_different_decision(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = Fixture(directory, approval_required=True)
            try:
                first = fx.engine.decide(
                    program_id="p-1", actor_id="a-1", resolution=fx.resolution
                )
                approval = fx.engine.approve(decision_id=first.decision.decision_id)
                second_resolution = replace(fx.resolution, request_id="req-2")
                second = fx.engine.decide(
                    program_id="p-1", actor_id="a-1", resolution=second_resolution
                )
                with self.assertRaises(ApprovalInvalid):
                    fx.engine.issue_execution_authority(
                        decision_id=second.decision.decision_id,
                        approval_id=approval.approval_id,
                    )
            finally:
                fx.close()

    def test_revocation_blocks_decision_and_preissued_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = Fixture(directory)
            try:
                first = fx.engine.decide(
                    program_id="p-1", actor_id="a-1", resolution=fx.resolution
                )
                receipt = fx.engine.issue_execution_authority(
                    decision_id=first.decision.decision_id
                )
                second = fx.engine.decide(
                    program_id="p-1", actor_id="a-1",
                    resolution=replace(fx.resolution, request_id="req-2"),
                )
                fx.store.revoke_grant("g-1")
                with self.assertRaises(AuthorityDenied):
                    fx.engine.issue_execution_authority(
                        decision_id=second.decision.decision_id
                    )
                with self.assertRaises(AuthorityDenied):
                    fx.engine.consume_execution_authority(receipt_id=receipt.receipt_id)
            finally:
                fx.close()

    def test_program_actor_capability_and_policy_currentness_are_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = Fixture(directory)
            try:
                program_decision = fx.engine.decide(
                    program_id="p-1", actor_id="a-1", resolution=fx.resolution
                )
                fx.programs.transition("p-1", ProgramStatus.ACTIVE, expected_revision=0)
                with self.assertRaises(StaleProgramRevision):
                    fx.engine.issue_execution_authority(
                        decision_id=program_decision.decision.decision_id
                    )
            finally:
                fx.close()

        with tempfile.TemporaryDirectory() as directory:
            fx = Fixture(directory)
            try:
                actor_decision = fx.engine.decide(
                    program_id="p-1", actor_id="a-1", resolution=fx.resolution
                )
                fx.actors.replace_binding("a-1", "binding-b", expected_generation=0)
                with self.assertRaises(StaleActorGeneration):
                    fx.engine.issue_execution_authority(
                        decision_id=actor_decision.decision.decision_id
                    )
            finally:
                fx.close()

        with tempfile.TemporaryDirectory() as directory:
            fx = Fixture(directory)
            try:
                capability_decision = fx.engine.decide(
                    program_id="p-1", actor_id="a-1", resolution=fx.resolution
                )
                fx.capabilities.replace_handler(
                    "workspace.read", "replacement-handler", expected_binding_revision=0
                )
                with self.assertRaises(StaleCapabilityBinding):
                    fx.engine.issue_execution_authority(
                        decision_id=capability_decision.decision.decision_id
                    )
            finally:
                fx.close()

        with tempfile.TemporaryDirectory() as directory:
            fx = Fixture(directory)
            try:
                policy_decision = fx.engine.decide(
                    program_id="p-1", actor_id="a-1", resolution=fx.resolution
                )
                fx.store.install_policy(PolicySnapshot(1, (), (), NOW))
                with self.assertRaises(IntegrityViolation):
                    fx.engine.issue_execution_authority(
                        decision_id=policy_decision.decision.decision_id
                    )
            finally:
                fx.close()

    def test_policy_can_deny_even_with_matching_grant(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = Fixture(directory, deny_effects=(EffectClass.OBSERVE,))
            try:
                context = fx.engine.decide(
                    program_id="p-1", actor_id="a-1", resolution=fx.resolution
                )
                self.assertIs(context.decision.decision, AuthorityDecisionKind.DENY)
                self.assertEqual(context.decision.rationale_code, "policy_denied_effect")
            finally:
                fx.close()

    def test_effect_ceiling_and_resource_scope_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = Fixture(
                directory,
                capability_id="workspace.write",
                effect_ceiling=EffectClass.OBSERVE,
            )
            try:
                context = fx.engine.decide(
                    program_id="p-1", actor_id="a-1", resolution=fx.resolution
                )
                self.assertIs(context.decision.decision, AuthorityDecisionKind.DENY)
            finally:
                fx.close()

        with tempfile.TemporaryDirectory() as directory:
            fx = Fixture(directory, resource_scope=("other/*",))
            try:
                context = fx.engine.decide(
                    program_id="p-1", actor_id="a-1", resolution=fx.resolution
                )
                self.assertIs(context.decision.decision, AuthorityDecisionKind.DENY)
            finally:
                fx.close()

    def test_authority_events_share_global_ledger_without_polluting_program_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = Fixture(directory)
            try:
                original = fx.programs.get("p-1")
                fx.engine.decide(
                    program_id="p-1", actor_id="a-1", resolution=fx.resolution
                )
                rebuilt = fx.programs.rebuild("p-1")
                self.assertEqual(rebuilt, original)
                authority_rows = fx.programs._db.execute(
                    "SELECT event_type, program_id FROM events WHERE event_type LIKE 'authority.%'"
                ).fetchall()
                self.assertTrue(authority_rows)
                self.assertTrue(all(row["program_id"] is None for row in authority_rows))
            finally:
                fx.close()

    def test_approval_consumption_rolls_back_if_execution_receipt_persistence_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = Fixture(directory, approval_required=True)
            try:
                context = fx.engine.decide(
                    program_id="p-1", actor_id="a-1", resolution=fx.resolution
                )
                approval = fx.engine.approve(decision_id=context.decision.decision_id)
                original_append = fx.store._append_event

                def fail_on_execution(event_type, payload, **kwargs):
                    if event_type == "authority.execution_issued":
                        raise RuntimeError("injected persistence fault")
                    return original_append(event_type, payload, **kwargs)

                fx.store._append_event = fail_on_execution
                with self.assertRaises(RuntimeError):
                    fx.engine.issue_execution_authority(
                        decision_id=context.decision.decision_id,
                        approval_id=approval.approval_id,
                    )
                fx.store._append_event = original_append
                self.assertEqual(
                    fx.store.get_approval(approval.approval_id),
                    approval,
                )
            finally:
                fx.close()

    def test_forged_resolution_cannot_change_declared_effect_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            fx = Fixture(directory)
            try:
                forged = replace(
                    fx.resolution,
                    resolved_effect=ResolvedEffect(
                        resource_type="workspace_path",
                        target="notes.txt",
                        effect_class=EffectClass.DELETE,
                        parameters={},
                    ),
                )
                with self.assertRaises(IntegrityViolation):
                    fx.engine.decide(
                        program_id="p-1", actor_id="a-1", resolution=forged
                    )
            finally:
                fx.close()


if __name__ == "__main__":
    unittest.main()
