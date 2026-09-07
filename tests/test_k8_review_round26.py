from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_capital.kernel.capability_store import CapabilityRepository, capability_descriptor
from ai_capital.kernel.context import ContextCompiler, ContextRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import EffectClass, Reversibility, RiskClass
from ai_capital.kernel.errors import IntegrityViolation
from ai_capital.kernel.models import Capability, Program


class K8ReviewRound26Tests(unittest.TestCase):
    @staticmethod
    def _capability(handler_binding: str) -> Capability:
        return Capability(
            capability_id="capability.long-handler",
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

    def test_long_accepted_handler_binding_fits_snapshot_storage_envelope(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "Capability handler storage envelope"))
                capabilities = CapabilityRepository(programs)
                capability = capabilities.register(self._capability("handler." + ("x" * 8192)))
                snapshot = capabilities.create_snapshot((capability_descriptor(capability),))
                contexts = ContextRepository(programs)
                compiler = ContextCompiler(contexts, capabilities=capabilities)

                compiled = compiler.compile(
                    program.program_id,
                    budget_units=100_000,
                    capability_snapshot=snapshot,
                )

                self.assertIn(
                    f"capability_snapshot:{snapshot.snapshot_id}",
                    compiled.receipt.included_refs,
                )
            finally:
                programs.close()

    def test_hidden_binding_storage_still_fails_before_snapshot_decode(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "Capability hidden storage gate"))
                capabilities = CapabilityRepository(programs)
                capability = capabilities.register(self._capability("handler.small"))
                snapshot = capabilities.create_snapshot((capability_descriptor(capability),))
                programs._db.execute(
                    """
                    UPDATE capability_bindings
                    SET capability_json = capability_json || ?
                    WHERE capability_id = ? AND binding_revision = ?
                    """,
                    (" " * 8192, capability.capability_id, capability.binding_revision),
                )
                contexts = ContextRepository(programs)
                compiler = ContextCompiler(contexts, capabilities=capabilities)

                with patch.object(
                    capabilities,
                    "get_snapshot",
                    side_effect=AssertionError("corrupt Capability binding was decoded"),
                ) as get_snapshot:
                    with self.assertRaises(IntegrityViolation):
                        compiler.compile(
                            program.program_id,
                            budget_units=100_000,
                            capability_snapshot=snapshot,
                        )
                get_snapshot.assert_not_called()
            finally:
                programs.close()


if __name__ == "__main__":
    unittest.main()
