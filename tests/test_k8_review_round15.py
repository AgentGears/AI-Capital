from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_capital.kernel.capability_store import (
    CapabilityRepository,
    capability_descriptor,
)
from ai_capital.kernel.context import ContextCompiler, ContextRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import EffectClass, Reversibility, RiskClass
from ai_capital.kernel.errors import ContextBudgetExceeded, IntegrityViolation
from ai_capital.kernel.evidence_store import EvidenceRepository
from ai_capital.kernel.models import Capability, Program


class K8ReviewRound15Tests(unittest.TestCase):
    def test_corrupt_oversized_artifact_is_rejected_before_recall_materialization(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "bounded artifact stat"))
                evidence = EvidenceRepository(programs)
                item = evidence.admit(
                    content=b"small",
                    source_class="test",
                    observed_at="2026-01-01T00:00:00Z",
                    provenance=("test",),
                    trust_class="test",
                    currentness="current",
                )
                artifact_path = evidence._artifact_path(item.digest)
                artifact_path.write_bytes(b"x" * 131072)
                contexts = ContextRepository(programs, evidence)
                with patch.object(
                    contexts,
                    "_resolve_recall",
                    side_effect=AssertionError("corrupt oversized Evidence was materialized"),
                ) as resolve:
                    with self.assertRaises(IntegrityViolation):
                        contexts.recall(
                            program.program_id,
                            (f"evidence:{item.evidence_id}",),
                            max_items=1,
                            max_units=100_000,
                        )
                resolve.assert_not_called()
            finally:
                programs.close()

    def test_corrupt_oversized_artifact_is_rejected_before_current_evidence_decode(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "bounded current artifact stat"))
                evidence = EvidenceRepository(programs)
                item = evidence.admit(
                    content=b"small",
                    source_class="test",
                    observed_at="2026-01-01T00:00:00Z",
                    provenance=("test",),
                    trust_class="test",
                    currentness="current",
                )
                evidence._artifact_path(item.digest).write_bytes(b"x" * 131072)
                contexts = ContextRepository(programs, evidence)
                compiler = ContextCompiler(contexts, evidence=evidence)
                with patch.object(
                    evidence,
                    "_row",
                    side_effect=AssertionError("corrupt current Evidence record was decoded"),
                ) as full_row:
                    with self.assertRaises(IntegrityViolation):
                        compiler.compile(
                            program.program_id,
                            budget_units=100_000,
                            evidence_refs=(item.evidence_id,),
                        )
                full_row.assert_not_called()
            finally:
                programs.close()

    def _large_snapshot(self, programs: ProgramRepository):
        capabilities = CapabilityRepository(programs)
        capability = capabilities.register(
            Capability(
                capability_id="capability.large",
                schema_version=1,
                operation="observe",
                resource_type="artifact",
                effect_class=EffectClass.OBSERVE,
                reversibility=Reversibility.REVERSIBLE,
                risk_class=RiskClass.LOW,
                input_schema={
                    "type": "object",
                    "properties": {
                        "x" * 131072: {"type": "string"},
                    },
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
                handler_binding="handler.large",
            )
        )
        snapshot = capabilities.create_snapshot((capability_descriptor(capability),))
        return capabilities, snapshot

    def test_oversized_capability_snapshot_is_rejected_before_durable_decode(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "bounded Capability preflight"))
                capabilities, snapshot = self._large_snapshot(programs)
                contexts = ContextRepository(programs)
                baseline = ContextCompiler(contexts).compile(
                    program.program_id,
                    budget_units=100_000,
                )
                compiler = ContextCompiler(contexts, capabilities=capabilities)
                with patch.object(
                    capabilities,
                    "get_snapshot",
                    side_effect=AssertionError("oversized Capability snapshot was decoded"),
                ) as get_snapshot:
                    with self.assertRaises(ContextBudgetExceeded):
                        compiler.compile(
                            program.program_id,
                            budget_units=baseline.used_units,
                            capability_snapshot=snapshot,
                        )
                get_snapshot.assert_not_called()
            finally:
                programs.close()

    def test_fitting_capability_snapshot_materializes_after_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "Capability fit control"))
                capabilities = CapabilityRepository(programs)
                capability = capabilities.register(
                    Capability(
                        capability_id="capability.small",
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
                        handler_binding="handler.small",
                    )
                )
                snapshot = capabilities.create_snapshot((capability_descriptor(capability),))
                contexts = ContextRepository(programs)
                compiler = ContextCompiler(contexts, capabilities=capabilities)
                with patch.object(
                    capabilities,
                    "get_snapshot",
                    wraps=capabilities.get_snapshot,
                ) as get_snapshot:
                    compiled = compiler.compile(
                        program.program_id,
                        budget_units=100_000,
                        capability_snapshot=snapshot,
                    )
                get_snapshot.assert_called_once_with(snapshot.snapshot_id)
                self.assertIn(
                    f"capability_snapshot:{snapshot.snapshot_id}",
                    compiled.receipt.included_refs,
                )
            finally:
                programs.close()


if __name__ == "__main__":
    unittest.main()
