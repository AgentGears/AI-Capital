from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_capital.kernel.actor_store import ActorRepository
from ai_capital.kernel.builtin_capabilities import install_builtin_capabilities
from ai_capital.kernel.capability_broker import CapabilityBroker, CapabilityHandlerRegistry
from ai_capital.kernel.capability_store import CapabilityRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ContextCompleteness
from ai_capital.kernel.errors import InvalidRequest
from ai_capital.kernel.inference import (
    InferenceHost,
    ModelBindingRegistry,
    capability_snapshot_ref,
)
from ai_capital.kernel.models import Actor, ContextReceipt, ModelTurn, Program
from ai_capital.kernel.serialization import canonical_json, to_canonical_data


class CapturingProvider:
    def __init__(self):
        self.requests = []

    def effective_configuration(self):
        return {"kind": "capture", "revision": 1}

    def generate(self, request):
        self.requests.append(request)
        return ModelTurn(provenance_receipt=request.attempt_id)


def receipt_for(program: Program, included_refs: tuple[str, ...]) -> ContextReceipt:
    return ContextReceipt(
        "ctx-1",
        program.program_id,
        program.revision,
        included_refs,
        (),
        ContextCompleteness.COMPLETE,
        1000,
        "2026-08-30T00:00:00Z",
    )


class CapabilityContextTests(unittest.TestCase):
    def _fixture(self, directory: str):
        path = Path(directory) / "kernel.db"
        programs = ProgramRepository(path)
        program = programs.create(Program("p-1", 0, "capability context"))
        actors = ActorRepository(programs)
        actors.register(Actor("actor-1", 0, "worker", "binding-a"))
        capabilities = CapabilityRepository(programs)
        handlers = CapabilityHandlerRegistry()
        install_builtin_capabilities(capabilities, handlers)
        broker = CapabilityBroker(capabilities, handlers)
        snapshot = broker.snapshot(("workspace.read", "artifact.write"))
        provider = CapturingProvider()
        bindings = ModelBindingRegistry()
        bindings.register("binding-a", provider)
        host = InferenceHost(programs, actors, bindings)
        return programs, actors, program, snapshot, provider, host

    def test_host_injects_exact_receipted_snapshot_into_durable_model_context(self):
        with tempfile.TemporaryDirectory() as directory:
            programs, actors, program, snapshot, provider, host = self._fixture(directory)
            try:
                context_receipt = receipt_for(
                    program,
                    (f"program:{program.program_id}", capability_snapshot_ref(snapshot)),
                )
                result = host.infer(
                    program_id=program.program_id,
                    actor_id="actor-1",
                    context_receipt=context_receipt,
                    context={"objective": program.objective},
                    capability_snapshot=snapshot,
                )
                self.assertEqual(len(provider.requests), 1)
                request = provider.requests[0]
                self.assertEqual(
                    canonical_json(request.context["capability_snapshot"]),
                    canonical_json(to_canonical_data(snapshot)),
                )
                self.assertEqual(
                    canonical_json(actors.request(result.receipt.attempt_id).context),
                    canonical_json(request.context),
                )
                first_descriptor = request.context["capability_snapshot"]["capabilities"][0]
                self.assertNotIn("handler_binding", first_descriptor)
            finally:
                programs.close()

    def test_snapshot_without_matching_receipt_reference_is_rejected_before_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            programs, _, program, snapshot, provider, host = self._fixture(directory)
            try:
                with self.assertRaises(InvalidRequest):
                    host.infer(
                        program_id=program.program_id,
                        actor_id="actor-1",
                        context_receipt=receipt_for(program, (f"program:{program.program_id}",)),
                        context={"objective": program.objective},
                        capability_snapshot=snapshot,
                    )
                self.assertEqual(provider.requests, [])
            finally:
                programs.close()

    def test_receipt_cannot_claim_snapshot_when_none_is_injected(self):
        with tempfile.TemporaryDirectory() as directory:
            programs, _, program, snapshot, provider, host = self._fixture(directory)
            try:
                with self.assertRaises(InvalidRequest):
                    host.infer(
                        program_id=program.program_id,
                        actor_id="actor-1",
                        context_receipt=receipt_for(
                            program,
                            (capability_snapshot_ref(snapshot),),
                        ),
                        context={"objective": program.objective},
                    )
                self.assertEqual(provider.requests, [])
            finally:
                programs.close()

    def test_caller_cannot_supply_reserved_capability_snapshot_context(self):
        with tempfile.TemporaryDirectory() as directory:
            programs, _, program, snapshot, provider, host = self._fixture(directory)
            try:
                with self.assertRaises(InvalidRequest):
                    host.infer(
                        program_id=program.program_id,
                        actor_id="actor-1",
                        context_receipt=receipt_for(
                            program,
                            (capability_snapshot_ref(snapshot),),
                        ),
                        context={"capability_snapshot": {"forged": True}},
                        capability_snapshot=snapshot,
                    )
                self.assertEqual(provider.requests, [])
            finally:
                programs.close()

    def test_different_snapshot_reference_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            programs, _, program, snapshot, provider, host = self._fixture(directory)
            try:
                with self.assertRaises(InvalidRequest):
                    host.infer(
                        program_id=program.program_id,
                        actor_id="actor-1",
                        context_receipt=receipt_for(
                            program,
                            ("capability_snapshot:not-the-supplied-snapshot",),
                        ),
                        context={"objective": program.objective},
                        capability_snapshot=snapshot,
                    )
                self.assertEqual(provider.requests, [])
            finally:
                programs.close()


if __name__ == "__main__":
    unittest.main()
