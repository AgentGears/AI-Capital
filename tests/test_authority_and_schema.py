from pathlib import Path
import dataclasses
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_capital.kernel.enums import (
    ActorStatus,
    AuthorityDecisionKind,
    AuthorityDomain,
    EffectClass,
    ProgramStatus,
    Reversibility,
    RiskClass,
)
from ai_capital.kernel.errors import AuthorityDenied, IntegrityViolation, StaleActorGeneration
from ai_capital.kernel.frozen_json import FrozenMap
from ai_capital.kernel.models import (
    Actor,
    AuthorityDecision,
    Capability,
    ExecutionAuthorityReceipt,
    ModelTurn,
    Program,
)
from ai_capital.kernel import models
from ai_capital.kernel.ownership import ownership_matrix, validate_execution_authority
from ai_capital.kernel.serialization import canonical_digest, canonical_json


EXPECTED_RECORDS = {
    "Program", "Actor", "Capability", "Grant", "AuthorityDecision", "Operation",
    "Evidence", "Claim", "Disposition", "Verification", "Event", "ContextReceipt",
    "ModelAttemptReceipt", "ExecutionAuthorityReceipt", "CompletionReceipt",
}


def fixture():
    program = Program("p-1", 3, "objective", status=ProgramStatus.ACTIVE)
    actor = Actor("a-1", 7, "worker", "binding-1", status=ActorStatus.ACTIVE)
    capability = Capability(
        "cap-1", 1, "workspace.read", "workspace", EffectClass.OBSERVE,
        Reversibility.REVERSIBLE, RiskClass.LOW, {}, {}, 5, "handler-1",
    )
    decision = AuthorityDecision(
        "d-1", "req-1", "workspace:/file", AuthorityDecisionKind.ALLOW,
        "within_scope", 11, ("g-1",), "2026-08-30T00:00:00Z",
    )
    receipt = ExecutionAuthorityReceipt(
        "r-1", "d-1", "p-1", 3, "a-1", 7, "cap-1", 5, 11,
        ("g-1",), canonical_digest(decision.resolved_effect),
        "2026-08-30T00:00:00Z", "single-use-1",
    )
    return program, actor, capability, decision, receipt


class AuthorityAndSchemaTests(unittest.TestCase):
    def test_k0_records_exist_and_are_frozen(self):
        for name in EXPECTED_RECORDS:
            record = getattr(models, name)
            self.assertTrue(dataclasses.is_dataclass(record), name)
            self.assertTrue(record.__dataclass_params__.frozen, name)

    def test_json_shaped_fields_are_deep_frozen(self):
        capability = Capability(
            "cap-1", 1, "workspace.read", "workspace", EffectClass.OBSERVE,
            Reversibility.REVERSIBLE, RiskClass.LOW,
            {"properties": {"path": ["a", "b"]}}, {}, 1, "handler-1",
        )
        self.assertIsInstance(capability.input_schema, FrozenMap)
        nested = capability.input_schema["properties"]
        self.assertIsInstance(nested, FrozenMap)
        self.assertEqual(nested["path"], ("a", "b"))
        with self.assertRaises(TypeError):
            capability.input_schema["new"] = "value"
        with self.assertRaises(AttributeError):
            capability.input_schema._items = ()

    def test_canonical_structures_reject_ambiguous_values(self):
        with self.assertRaises(TypeError):
            FrozenMap({1: "not-a-string-key"})
        with self.assertRaises(ValueError):
            canonical_json({"value": float("nan")})

    def test_every_authority_domain_has_owner(self):
        self.assertEqual(set(ownership_matrix()), set(AuthorityDomain))

    def test_current_execution_authority_validates(self):
        program, actor, capability, decision, receipt = fixture()
        validate_execution_authority(
            receipt=receipt, decision=decision, program=program, actor=actor,
            capability=capability, policy_revision=11,
        )

    def test_stale_actor_generation_rejected(self):
        program, actor, capability, decision, receipt = fixture()
        actor = Actor("a-1", 8, "worker", "binding-2")
        with self.assertRaises(StaleActorGeneration):
            validate_execution_authority(
                receipt=receipt, decision=decision, program=program, actor=actor,
                capability=capability, policy_revision=11,
            )

    def test_non_allow_decision_rejected(self):
        program, actor, capability, decision, receipt = fixture()
        denied = dataclasses.replace(decision, decision=AuthorityDecisionKind.DENY)
        with self.assertRaises(AuthorityDenied):
            validate_execution_authority(
                receipt=receipt, decision=denied, program=program, actor=actor,
                capability=capability, policy_revision=11,
            )

    def test_resolved_effect_mismatch_rejected(self):
        program, actor, capability, decision, receipt = fixture()
        receipt = dataclasses.replace(receipt, resolved_effect_digest="wrong")
        with self.assertRaises(IntegrityViolation):
            validate_execution_authority(
                receipt=receipt, decision=decision, program=program, actor=actor,
                capability=capability, policy_revision=11,
            )

    def test_model_turn_has_only_proposal_surfaces(self):
        names = {field.name for field in dataclasses.fields(ModelTurn)}
        self.assertEqual(names, {
            "reasoning_proposals", "capability_requests", "claim_proposals",
            "completion_proposal", "provenance_receipt",
        })
        with self.assertRaises(TypeError):
            ModelTurn()

    def test_canonical_serialization_is_deterministic(self):
        left = {"b": 2, "a": 1}
        right = {"a": 1, "b": 2}
        self.assertEqual(canonical_json(left), canonical_json(right))
        self.assertEqual(canonical_digest(left), canonical_digest(right))


if __name__ == "__main__":
    unittest.main()
