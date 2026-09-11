from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_capital.kernel.actor_store import ActorRepository
from ai_capital.kernel.bounded_inference import BoundedInferenceHost
from ai_capital.kernel.context import ContextCompiler, ContextRepository, evidence_ref
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ContextCompleteness
from ai_capital.kernel.evidence_store import EvidenceRepository
from ai_capital.kernel.inference import ModelBindingRegistry
from ai_capital.kernel.models import Actor, ModelTurn, Program


OBSERVED = "2026-09-05T00:00:00Z"


class CapturingProvider:
    def __init__(self):
        self.requests = []

    def effective_configuration(self):
        return {"kind": "capture", "revision": 1}

    def generate(self, request):
        self.requests.append(request)
        return ModelTurn(provenance_receipt=request.attempt_id)


class K8ReviewRound7Tests(unittest.TestCase):
    def test_inference_validates_only_requested_receipt_on_hot_path(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "bounded receipt validation"))
                contexts = ContextRepository(programs)
                compiler = ContextCompiler(contexts)
                for _ in range(5):
                    compiler.compile(program.program_id, budget_units=100_000)
                compiled = compiler.compile(program.program_id, budget_units=100_000)

                actors = ActorRepository(programs)
                actors.register(Actor("actor-1", 0, "worker", "binding-a"))
                provider = CapturingProvider()
                bindings = ModelBindingRegistry()
                bindings.register("binding-a", provider)
                host = BoundedInferenceHost(programs, actors, bindings, contexts)

                with patch.object(
                    contexts,
                    "_semantic_receipts",
                    side_effect=AssertionError("hot-path inference scanned Host Context history"),
                ) as semantic_receipts:
                    result = host.infer(
                        program_id=program.program_id,
                        actor_id="actor-1",
                        context_receipt=compiled.receipt,
                        context=compiled.context,
                    )

                semantic_receipts.assert_not_called()
                self.assertEqual(len(provider.requests), 1)
                self.assertEqual(
                    result.receipt.context_receipt_ref,
                    compiled.receipt.context_receipt_id,
                )
            finally:
                programs.close()

    def test_oversized_current_evidence_is_excluded_before_artifact_materialization(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "bounded Evidence compilation"))
                evidence = EvidenceRepository(programs)
                admitted = tuple(
                    evidence.admit(
                        content=((f"artifact-{index}-".encode("ascii")) + b"x" * 131072),
                        source_class="fixture_observation",
                        observed_at=OBSERVED,
                        provenance=("fixture:source", "admission:host"),
                        trust_class="observed",
                        currentness="current",
                    )
                    for index in range(3)
                )
                contexts = ContextRepository(programs, evidence)
                compiler = ContextCompiler(contexts, evidence=evidence)
                baseline = compiler.compile(program.program_id, budget_units=100_000)

                with patch.object(
                    evidence,
                    "get",
                    wraps=evidence.get,
                ) as get_evidence, patch.object(
                    evidence,
                    "artifact",
                    wraps=evidence.artifact,
                ) as artifact, patch.object(
                    evidence,
                    "_read_artifact",
                    wraps=evidence._read_artifact,
                ) as read_artifact, patch.object(
                    contexts,
                    "_semantic_receipts",
                    side_effect=AssertionError("compilation scanned Host Context history"),
                ) as semantic_receipts:
                    compiled = compiler.compile(
                        program.program_id,
                        budget_units=baseline.used_units,
                        evidence_refs=tuple(item.evidence_id for item in reversed(admitted)),
                    )

                get_evidence.assert_not_called()
                artifact.assert_not_called()
                read_artifact.assert_not_called()
                semantic_receipts.assert_not_called()
                self.assertIs(compiled.receipt.completeness, ContextCompleteness.TRUNCATED)
                self.assertEqual(
                    set(compiled.receipt.excluded_refs),
                    {evidence_ref(item.evidence_id) for item in admitted},
                )
                self.assertEqual(compiled.used_units, baseline.used_units)
            finally:
                programs.close()


if __name__ == "__main__":
    unittest.main()
