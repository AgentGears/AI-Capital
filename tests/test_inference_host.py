from pathlib import Path
import inspect
import sys
import tempfile
import unittest
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_capital.kernel.actor_store import ActorRepository
from ai_capital.kernel.deterministic_provider import DeterministicInferenceProvider
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ContextCompleteness, ModelAttemptOutcome, ProgramStatus
from ai_capital.kernel.errors import (
    IntegrityViolation,
    InternalFault,
    InvalidRequest,
    StaleActorGeneration,
    StaleProgramRevision,
)
from ai_capital.kernel.inference import InferenceHost, ModelBindingRegistry
from ai_capital.kernel.models import Actor, ContextReceipt, ModelTurn, Program, ReasoningProposal
from ai_capital.kernel.serialization import canonical_digest


def context_receipt(program: Program, identity: str = "ctx-1") -> ContextReceipt:
    return ContextReceipt(
        identity,
        program.program_id,
        program.revision,
        (f"program:{program.program_id}",),
        (),
        ContextCompleteness.COMPLETE,
        100,
        "2026-08-30T00:00:00Z",
    )


class RaisingProvider:
    def effective_configuration(self):
        return {"kind": "raising", "revision": 1}

    def generate(self, request):
        raise RuntimeError("provider-side failure")


class InvalidOutputProvider:
    def effective_configuration(self):
        return {"kind": "invalid-output", "revision": 1}

    def generate(self, request):
        return ModelTurn(provenance_receipt="not-the-host-attempt")


class CountingProvider:
    def __init__(self):
        self.calls = 0

    def effective_configuration(self):
        return {"kind": "counting", "revision": 1}

    def generate(self, request):
        self.calls += 1
        return ModelTurn(provenance_receipt=request.attempt_id)


class ProgramMutatingProvider:
    def __init__(self, programs):
        self.programs = programs

    def effective_configuration(self):
        return {"kind": "program-mutating-test", "revision": 1}

    def generate(self, request):
        self.programs.transition(
            request.program_id,
            ProgramStatus.ACTIVE,
            expected_revision=request.program_revision,
        )
        return ModelTurn(
            provenance_receipt=request.attempt_id,
            reasoning_proposals=(ReasoningProposal("stale program output"),),
        )


class ActorReplacingProvider:
    def __init__(self, actors):
        self.actors = actors

    def effective_configuration(self):
        return {"kind": "actor-replacing-test", "revision": 1}

    def generate(self, request):
        self.actors.replace_binding(
            request.actor_id,
            "binding-next",
            expected_generation=request.actor_generation,
        )
        return ModelTurn(
            provenance_receipt=request.attempt_id,
            reasoning_proposals=(ReasoningProposal("stale actor output"),),
        )


class InferenceHostTests(unittest.TestCase):
    def test_actor_model_binding_can_be_replaced_without_changing_program(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kernel.db"
            with ProgramRepository(path) as programs:
                program = programs.create(Program("p-1", 0, "replace cognition"))
                actors = ActorRepository(programs)
                actors.register(Actor("actor-1", 0, "worker", "binding-a"))
                bindings = ModelBindingRegistry()
                bindings.register("binding-a", DeterministicInferenceProvider("first"))
                bindings.register("binding-b", DeterministicInferenceProvider("second"))
                host = InferenceHost(programs, actors, bindings)

                first = host.infer(
                    program_id="p-1",
                    actor_id="actor-1",
                    context_receipt=context_receipt(program, "ctx-a"),
                    context={"objective": program.objective},
                )
                replaced = actors.replace_binding(
                    "actor-1", "binding-b", expected_generation=0
                )
                second = host.infer(
                    program_id="p-1",
                    actor_id="actor-1",
                    context_receipt=context_receipt(program, "ctx-b"),
                    context={"objective": program.objective},
                )

                self.assertEqual(programs.get("p-1"), program)
                self.assertEqual(replaced.actor_id, "actor-1")
                self.assertEqual(replaced.generation, 1)
                self.assertEqual(
                    first.turn.reasoning_proposals[0].text,
                    "first",
                )
                self.assertEqual(
                    second.turn.reasoning_proposals[0].text,
                    "second",
                )
                self.assertEqual(
                    tuple(receipt.actor_generation for receipt in actors.attempts("actor-1")),
                    (0, 1),
                )
                UUID(first.receipt.attempt_id)
                UUID(second.receipt.attempt_id)
                self.assertEqual(
                    first.receipt.effective_config_digest,
                    canonical_digest({"kind": "deterministic", "revision": 1}),
                )

            with ProgramRepository(path) as programs:
                actors = ActorRepository(programs)
                self.assertEqual(actors.get("actor-1").generation, 1)
                self.assertEqual(len(actors.attempts("actor-1")), 2)

    def test_attempt_identity_is_host_owned_not_caller_supplied(self):
        parameters = inspect.signature(InferenceHost.infer).parameters
        self.assertNotIn("attempt_id", parameters)

    def test_stale_context_fails_before_provider_invocation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kernel.db"
            with ProgramRepository(path) as programs:
                created = programs.create(Program("p-1", 0, "stale context"))
                stale = context_receipt(created)
                programs.transition("p-1", ProgramStatus.ACTIVE, expected_revision=0)
                actors = ActorRepository(programs)
                actors.register(Actor("actor-1", 0, "worker", "binding-a"))
                provider = CountingProvider()
                bindings = ModelBindingRegistry()
                bindings.register("binding-a", provider)
                host = InferenceHost(programs, actors, bindings)
                with self.assertRaises(InvalidRequest):
                    host.infer(
                        program_id="p-1",
                        actor_id="actor-1",
                        context_receipt=stale,
                        context={"objective": "stale context"},
                    )
                self.assertEqual(provider.calls, 0)
                self.assertEqual(actors.attempts("actor-1"), ())

    def test_provider_failure_is_receipted_without_program_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kernel.db"
            with ProgramRepository(path) as programs:
                program = programs.create(Program("p-1", 0, "provider failure"))
                actors = ActorRepository(programs)
                actors.register(Actor("actor-1", 0, "worker", "binding-a"))
                bindings = ModelBindingRegistry()
                bindings.register("binding-a", RaisingProvider())
                host = InferenceHost(programs, actors, bindings)
                with self.assertRaises(InternalFault):
                    host.infer(
                        program_id="p-1",
                        actor_id="actor-1",
                        context_receipt=context_receipt(program),
                        context={"objective": program.objective},
                    )
                receipt = actors.attempts("actor-1")[-1]
                self.assertEqual(receipt.outcome, ModelAttemptOutcome.FAILED)
                self.assertEqual(receipt.error_code, "provider_failure")
                self.assertIsNone(actors.turn(receipt.attempt_id))
                self.assertEqual(programs.get("p-1"), program)

    def test_invalid_output_provenance_is_rejected_and_receipted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kernel.db"
            with ProgramRepository(path) as programs:
                program = programs.create(Program("p-1", 0, "invalid output"))
                actors = ActorRepository(programs)
                actors.register(Actor("actor-1", 0, "worker", "binding-a"))
                bindings = ModelBindingRegistry()
                bindings.register("binding-a", InvalidOutputProvider())
                host = InferenceHost(programs, actors, bindings)
                with self.assertRaises(IntegrityViolation):
                    host.infer(
                        program_id="p-1",
                        actor_id="actor-1",
                        context_receipt=context_receipt(program),
                        context={"objective": program.objective},
                    )
                receipt = actors.attempts("actor-1")[-1]
                self.assertEqual(receipt.outcome, ModelAttemptOutcome.FAILED)
                self.assertEqual(receipt.error_code, "invalid_model_output")
                self.assertIsNone(actors.turn(receipt.attempt_id))
                self.assertEqual(programs.get("p-1"), program)

    def test_program_revision_change_during_inference_makes_output_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kernel.db"
            with ProgramRepository(path) as programs:
                program = programs.create(Program("p-1", 0, "stale result"))
                actors = ActorRepository(programs)
                actors.register(Actor("actor-1", 0, "worker", "binding-a"))
                bindings = ModelBindingRegistry()
                bindings.register("binding-a", ProgramMutatingProvider(programs))
                host = InferenceHost(programs, actors, bindings)
                with self.assertRaises(StaleProgramRevision):
                    host.infer(
                        program_id="p-1",
                        actor_id="actor-1",
                        context_receipt=context_receipt(program),
                        context={"objective": program.objective},
                    )
                receipt = actors.attempts("actor-1")[-1]
                self.assertEqual(receipt.outcome, ModelAttemptOutcome.STALE)
                self.assertEqual(receipt.error_code, "stale_program_revision")
                self.assertIsNotNone(actors.turn(receipt.attempt_id))
                self.assertEqual(programs.get("p-1").revision, 1)

    def test_actor_generation_change_during_inference_makes_output_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kernel.db"
            with ProgramRepository(path) as programs:
                program = programs.create(Program("p-1", 0, "stale actor result"))
                actors = ActorRepository(programs)
                actors.register(Actor("actor-1", 0, "worker", "binding-a"))
                bindings = ModelBindingRegistry()
                bindings.register("binding-a", ActorReplacingProvider(actors))
                host = InferenceHost(programs, actors, bindings)
                with self.assertRaises(StaleActorGeneration):
                    host.infer(
                        program_id="p-1",
                        actor_id="actor-1",
                        context_receipt=context_receipt(program),
                        context={"objective": program.objective},
                    )
                receipt = actors.attempts("actor-1")[-1]
                self.assertEqual(receipt.outcome, ModelAttemptOutcome.STALE)
                self.assertEqual(receipt.error_code, "stale_actor_generation")
                self.assertIsNotNone(actors.turn(receipt.attempt_id))
                self.assertEqual(actors.get("actor-1").generation, 1)


if __name__ == "__main__":
    unittest.main()
