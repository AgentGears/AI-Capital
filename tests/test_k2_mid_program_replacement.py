from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_capital.kernel.actor_store import ActorRepository
from ai_capital.kernel.deterministic_provider import DeterministicInferenceProvider
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ContextCompleteness, ProgramStatus, WorkItemStatus
from ai_capital.kernel.inference import InferenceHost, ModelBindingRegistry
from ai_capital.kernel.models import Actor, ContextReceipt, Program, WorkItem


def context_receipt(program: Program, identity: str) -> ContextReceipt:
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


class MidProgramReplacementTests(unittest.TestCase):
    def test_replace_model_mid_program_without_changing_program_or_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kernel.db"
            with ProgramRepository(path) as programs:
                program = programs.create(Program("p-1", 0, "multi-step work"))
                program = programs.transition(
                    "p-1",
                    ProgramStatus.ACTIVE,
                    expected_revision=program.revision,
                )
                program = programs.add_work(
                    "p-1",
                    WorkItem("w-1", "first bounded step"),
                    expected_revision=program.revision,
                )
                program = programs.add_work(
                    "p-1",
                    WorkItem("w-2", "second bounded step"),
                    expected_revision=program.revision,
                )
                program = programs.satisfy_work(
                    "p-1",
                    "w-1",
                    expected_revision=program.revision,
                )
                canonical_before = program
                event_history_before = programs.list_events("p-1")

                actors = ActorRepository(programs)
                actors.register(
                    Actor(
                        "actor-1",
                        0,
                        "worker",
                        "binding-a",
                        grant_refs=("grant-1",),
                    )
                )
                bindings = ModelBindingRegistry()
                bindings.register("binding-a", DeterministicInferenceProvider("continue A"))
                bindings.register("binding-b", DeterministicInferenceProvider("continue B"))
                host = InferenceHost(programs, actors, bindings)

                first = host.infer(
                    program_id="p-1",
                    actor_id="actor-1",
                    context_receipt=context_receipt(program, "ctx-a"),
                    context={"objective": program.objective, "outstanding": ["w-2"]},
                )
                actors.replace_binding("actor-1", "binding-b", expected_generation=0)
                second = host.infer(
                    program_id="p-1",
                    actor_id="actor-1",
                    context_receipt=context_receipt(program, "ctx-b"),
                    context={"objective": program.objective, "outstanding": ["w-2"]},
                )

                self.assertEqual(first.turn.reasoning_proposals[0].text, "continue A")
                self.assertEqual(second.turn.reasoning_proposals[0].text, "continue B")
                self.assertEqual(programs.get("p-1"), canonical_before)
                self.assertEqual(programs.list_events("p-1"), event_history_before)
                self.assertEqual(canonical_before.work_items[0].status, WorkItemStatus.SATISFIED)
                self.assertEqual(canonical_before.work_items[1].status, WorkItemStatus.OPEN)
                self.assertEqual(actors.get("actor-1").grant_refs, ("grant-1",))


if __name__ == "__main__":
    unittest.main()
