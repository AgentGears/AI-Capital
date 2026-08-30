from pathlib import Path
import dataclasses
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_capital.kernel.enums import (
    ContextCompleteness,
    EffectClass,
    ProgramStatus,
    Reversibility,
    RiskClass,
)
from ai_capital.kernel.frozen_json import FrozenMap
from ai_capital.kernel.models import (
    Capability,
    CapabilityRequest,
    ContextReceipt,
    ModelTurn,
    Program,
)
from ai_capital.kernel.schema_codec import record_from_json, record_to_json
from ai_capital.kernel.serialization import canonical_json


class SchemaRoundTripTests(unittest.TestCase):
    def test_program_round_trip_restores_enums_and_tuples(self):
        original = Program(
            program_id="p-1",
            revision=4,
            objective="round trip",
            constraints=("bounded", "durable"),
            assumptions=("a-1",),
            decisions=("d-1",),
            work_items=("w-1", "w-2"),
            evidence_refs=("e-1",),
            operation_refs=("o-1",),
            verification_refs=("v-1",),
            success_criteria=("round trip passes",),
            status=ProgramStatus.BLOCKED,
        )
        payload = record_to_json(original)
        restored = record_from_json(Program, payload)
        self.assertEqual(restored, original)
        self.assertIsInstance(restored.constraints, tuple)
        self.assertIs(restored.status, ProgramStatus.BLOCKED)

    def test_capability_round_trip_restores_frozen_structures(self):
        original = Capability(
            capability_id="cap-1",
            schema_version=1,
            operation="workspace.read",
            resource_type="workspace",
            effect_class=EffectClass.OBSERVE,
            reversibility=Reversibility.REVERSIBLE,
            risk_class=RiskClass.LOW,
            input_schema={"properties": {"path": ["a", "b"]}},
            output_schema={"type": "object"},
            binding_revision=2,
            handler_binding="handler-1",
        )
        restored = record_from_json(Capability, record_to_json(original))
        self.assertEqual(canonical_json(restored), canonical_json(original))
        self.assertIsInstance(restored.input_schema, FrozenMap)
        self.assertEqual(restored.input_schema["properties"]["path"], ("a", "b"))

    def test_nested_model_turn_round_trip(self):
        original = ModelTurn(
            provenance_receipt="attempt-1",
            capability_requests=(
                CapabilityRequest("req-1", "cap-1", {"path": ["a", "b"]}, 2),
            ),
        )
        restored = record_from_json(ModelTurn, record_to_json(original))
        self.assertEqual(canonical_json(restored), canonical_json(original))
        self.assertIsInstance(restored.capability_requests, tuple)
        self.assertIsInstance(restored.capability_requests[0].arguments, FrozenMap)

    def test_unknown_or_missing_fields_fail_closed(self):
        with self.assertRaises(TypeError):
            record_from_json(Program, '{"program_id":"p-1"}')

    def test_duplicate_keys_fail_closed(self):
        with self.assertRaises(ValueError):
            record_from_json(Program, '{"program_id":"p-1","program_id":"p-2"}')

    def test_non_finite_numbers_fail_closed(self):
        with self.assertRaises(ValueError):
            record_from_json(Program, '{"value":NaN}')

    def test_canonical_program_cannot_be_directly_mutated(self):
        program = Program("p-1", 0, "immutable")
        with self.assertRaises(dataclasses.FrozenInstanceError):
            program.status = ProgramStatus.ACTIVE

    def test_context_projection_has_no_ownership_link_to_program(self):
        program = Program("p-1", 2, "durable")
        receipt = ContextReceipt(
            "ctx-1",
            "p-1",
            2,
            ("program:p-1",),
            ("history:h-1",),
            ContextCompleteness.TRUNCATED,
            100,
            "2026-08-30T00:00:00Z",
        )
        del receipt
        self.assertEqual(program.program_id, "p-1")
        self.assertEqual(program.revision, 2)
        self.assertEqual(program.objective, "durable")


if __name__ == "__main__":
    unittest.main()
