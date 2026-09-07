from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from ai_capital.kernel.actor_store import ActorRepository
from ai_capital.kernel.bounded_inference import BoundedInferenceHost
from ai_capital.kernel.context import ContextCompiler, ContextRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ContextPriority
from ai_capital.kernel.errors import IntegrityViolation
from ai_capital.kernel.inference import ModelBindingRegistry
from ai_capital.kernel.models import Actor, ModelTurn, Program


class CapturingProvider:
    def __init__(self):
        self.requests = []

    def effective_configuration(self):
        return {"kind": "capture", "revision": 1}

    def generate(self, request):
        self.requests.append(request)
        return ModelTurn(provenance_receipt=request.attempt_id)


class K8ReviewRound25Tests(unittest.TestCase):
    def _host(self, directory: str):
        programs = ProgramRepository(Path(directory) / "host.db")
        program = programs.create(Program("p-1", 0, "Host-control receipt freshness"))
        contexts = ContextRepository(programs)
        compiler = ContextCompiler(contexts)
        actors = ActorRepository(programs)
        actors.register(Actor("actor-1", 0, "worker", "binding-a"))
        provider = CapturingProvider()
        bindings = ModelBindingRegistry()
        bindings.register("binding-a", provider)
        host = BoundedInferenceHost(programs, actors, bindings, contexts)
        return programs, program, contexts, compiler, provider, host

    def test_new_host_control_invalidates_preexisting_context_receipt_before_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            programs, program, contexts, compiler, provider, host = self._host(directory)
            try:
                stale = compiler.compile(program.program_id, budget_units=100_000)
                contexts.persist_source(
                    program.program_id,
                    priority=ContextPriority.HOST_CONTROL,
                    payload={"control": "new mandatory directive"},
                )

                with self.assertRaises(IntegrityViolation):
                    host.infer(
                        program_id=program.program_id,
                        actor_id="actor-1",
                        context_receipt=stale.receipt,
                        context=stale.context,
                    )
                self.assertEqual(provider.requests, [])
            finally:
                programs.close()

    def test_receipt_compiled_after_host_control_change_remains_inference_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            programs, program, contexts, compiler, provider, host = self._host(directory)
            try:
                control_ref = contexts.persist_source(
                    program.program_id,
                    priority=ContextPriority.HOST_CONTROL,
                    payload={"control": "current mandatory directive"},
                )
                current = compiler.compile(program.program_id, budget_units=100_000)
                self.assertIn(control_ref, current.receipt.included_refs)

                host.infer(
                    program_id=program.program_id,
                    actor_id="actor-1",
                    context_receipt=current.receipt,
                    context=current.context,
                )
                self.assertEqual(len(provider.requests), 1)
            finally:
                programs.close()


if __name__ == "__main__":
    unittest.main()
