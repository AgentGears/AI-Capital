from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from ai_capital.kernel.capability_store import CapabilityRepository, capability_descriptor
from ai_capital.kernel.context import ContextCompiler, ContextRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import EffectClass, Reversibility, RiskClass
from ai_capital.kernel.errors import InvalidRequest
from ai_capital.kernel.models import Capability, Program


def _capability(*, handler_binding: str, capability_id: str = "capability.bounded-handler") -> Capability:
    return Capability(
        capability_id=capability_id,
        schema_version=1,
        operation="observe",
        resource_type="artifact",
        effect_class=EffectClass.OBSERVE,
        reversibility=Reversibility.REVERSIBLE,
        risk_class=RiskClass.LOW,
        input_schema={
            "type": "object",
            "properties": {},
            "required": (),
            "additional_properties": False,
        },
        output_schema={
            "type": "object",
            "properties": {},
            "required": (),
            "additional_properties": False,
        },
        binding_revision=0,
        handler_binding=handler_binding,
    )


class K8ReviewRound26Tests(unittest.TestCase):
    def test_registration_rejects_handler_binding_outside_bounded_envelope(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                capabilities = CapabilityRepository(programs)
                with self.assertRaisesRegex(
                    InvalidRequest,
                    "handler binding exceeds bounded storage envelope",
                ):
                    capabilities.register(_capability(handler_binding="h" * 8192))

                binding_count = programs._db.execute(
                    "SELECT COUNT(*) FROM capability_bindings"
                ).fetchone()[0]
                projection_count = programs._db.execute(
                    "SELECT COUNT(*) FROM capability_projections"
                ).fetchone()[0]
                self.assertEqual(binding_count, 0)
                self.assertEqual(projection_count, 0)
            finally:
                programs.close()

    def test_handler_replacement_cannot_bypass_bounded_envelope(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                capabilities = CapabilityRepository(programs)
                original = capabilities.register(_capability(handler_binding="handler.initial"))

                with self.assertRaisesRegex(
                    InvalidRequest,
                    "handler binding exceeds bounded storage envelope",
                ):
                    capabilities.replace_handler(
                        original.capability_id,
                        "h" * 8192,
                        expected_binding_revision=0,
                    )

                current = capabilities.get(original.capability_id)
                self.assertEqual(current.binding_revision, 0)
                self.assertEqual(current.handler_binding, "handler.initial")
                binding_count = programs._db.execute(
                    "SELECT COUNT(*) FROM capability_bindings WHERE capability_id = ?",
                    (original.capability_id,),
                ).fetchone()[0]
                self.assertEqual(binding_count, 1)
            finally:
                programs.close()

    def test_large_handler_within_bounded_envelope_remains_compilable(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "bounded handler positive control"))
                capabilities = CapabilityRepository(programs)
                capability = capabilities.register(_capability(handler_binding="h" * 3500))
                snapshot = capabilities.create_snapshot((capability_descriptor(capability),))
                contexts = ContextRepository(programs)

                compiled = ContextCompiler(contexts, capabilities=capabilities).compile(
                    program.program_id,
                    budget_units=100000,
                    capability_snapshot=snapshot,
                )

                self.assertEqual(
                    compiled.context["capability_snapshot"]["snapshot_id"],
                    snapshot.snapshot_id,
                )
            finally:
                programs.close()


if __name__ == "__main__":
    unittest.main()
