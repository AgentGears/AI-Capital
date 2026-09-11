from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_capital.kernel.actor_store import ActorRepository
from ai_capital.kernel.bounded_inference import BoundedInferenceHost
from ai_capital.kernel.builtin_capabilities import install_builtin_capabilities
from ai_capital.kernel.capability_broker import CapabilityBroker, CapabilityHandlerRegistry
from ai_capital.kernel.capability_store import CapabilityRepository
from ai_capital.kernel.context import ContextCompiler, ContextRepository, evidence_ref
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ContextCompleteness, ContextPriority
from ai_capital.kernel.errors import (
    ContextBudgetExceeded,
    ContextIncomplete,
    IntegrityViolation,
)
from ai_capital.kernel.evidence_store import EvidenceRepository
from ai_capital.kernel.inference import ModelBindingRegistry
from ai_capital.kernel.models import Actor, ModelTurn, Program
from ai_capital.kernel.serialization import canonical_json, to_canonical_data


OBSERVED = "2026-09-02T00:00:00Z"


class CapturingProvider:
    def __init__(self):
        self.requests = []

    def effective_configuration(self):
        return {"kind": "capture", "revision": 1}

    def generate(self, request):
        self.requests.append(request)
        return ModelTurn(provenance_receipt=request.attempt_id)


class K8BoundedContextTests(unittest.TestCase):
    def _base(self, directory: str):
        programs = ProgramRepository(Path(directory) / "kernel.db")
        program = programs.create(Program("p-1", 0, "bounded Context proof"))
        contexts = ContextRepository(programs)
        compiler = ContextCompiler(contexts)
        return programs, program, contexts, compiler

    def test_budget_truncation_is_deterministic_over_priority_and_stable_refs(self):
        with tempfile.TemporaryDirectory() as directory:
            programs, program, contexts, compiler = self._base(directory)
            try:
                first_ref = contexts.persist_source(
                    program.program_id,
                    priority=ContextPriority.ADVISORY_MEMORY,
                    payload={"name": "first", "text": "a" * 300},
                )
                second_ref = contexts.persist_source(
                    program.program_id,
                    priority=ContextPriority.ADVISORY_MEMORY,
                    payload={"name": "second", "text": "b" * 300},
                )
                one = compiler.compile(
                    program.program_id,
                    budget_units=100_000,
                    source_refs=(first_ref,),
                )
                budget = one.used_units

                left = compiler.compile(
                    program.program_id,
                    budget_units=budget,
                    source_refs=(first_ref, second_ref),
                )
                right = compiler.compile(
                    program.program_id,
                    budget_units=budget,
                    source_refs=(second_ref, first_ref),
                )

                self.assertIs(left.receipt.completeness, ContextCompleteness.TRUNCATED)
                self.assertEqual(left.receipt.included_refs, right.receipt.included_refs)
                self.assertEqual(left.receipt.excluded_refs, right.receipt.excluded_refs)
                self.assertEqual(len(left.receipt.excluded_refs), 1)
                self.assertLessEqual(left.used_units, budget)
            finally:
                programs.close()

    def test_mandatory_host_control_cannot_be_silently_evicted(self):
        with tempfile.TemporaryDirectory() as directory:
            programs, program, contexts, compiler = self._base(directory)
            try:
                baseline = compiler.compile(program.program_id, budget_units=100_000)
                control_ref = contexts.persist_source(
                    program.program_id,
                    priority=ContextPriority.HOST_CONTROL,
                    payload={"control": "x" * 2_000},
                )
                with self.assertRaises(ContextBudgetExceeded):
                    compiler.compile(
                        program.program_id,
                        budget_units=baseline.used_units,
                        source_refs=(control_ref,),
                    )
            finally:
                programs.close()

    def test_evicted_source_remains_durable_and_exactly_recallable_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kernel.db"
            programs = ProgramRepository(path)
            program = programs.create(Program("p-1", 0, "restart recall proof"))
            contexts = ContextRepository(programs)
            compiler = ContextCompiler(contexts)
            first_ref = contexts.persist_source(
                program.program_id,
                priority=ContextPriority.ADVISORY_MEMORY,
                payload={"name": "first", "text": "a" * 300},
            )
            second_ref = contexts.persist_source(
                program.program_id,
                priority=ContextPriority.ADVISORY_MEMORY,
                payload={"name": "second", "text": "b" * 300},
            )
            one = compiler.compile(
                program.program_id,
                budget_units=100_000,
                source_refs=(first_ref,),
            )
            truncated = compiler.compile(
                program.program_id,
                budget_units=one.used_units,
                source_refs=(first_ref, second_ref),
            )
            self.assertEqual(len(truncated.receipt.excluded_refs), 1)
            excluded_ref = truncated.receipt.excluded_refs[0]
            exact_before = contexts.persisted_source(program.program_id, excluded_ref)
            programs.close()

            programs = ProgramRepository(path)
            try:
                contexts = ContextRepository(programs)
                exact_after = contexts.persisted_source(program.program_id, excluded_ref)
                self.assertEqual(exact_after.payload, exact_before.payload)
                recalled = contexts.recall(
                    program.program_id,
                    (excluded_ref,),
                    max_items=1,
                    max_units=100_000,
                )
                self.assertIs(recalled.completeness, ContextCompleteness.COMPLETE)
                self.assertEqual(recalled.items[0].payload, exact_before.payload)
                self.assertIs(
                    recalled.items[0].priority,
                    ContextPriority.RECALLED_HISTORY,
                )
                self.assertEqual(recalled.items[0].authority, "historical_advisory")
                self.assertTrue(recalled.items[0].historical)
            finally:
                programs.close()

    def test_context_projection_loss_rebuilds_from_exact_semantic_event(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kernel.db"
            programs = ProgramRepository(path)
            program = programs.create(Program("p-1", 0, "Context rebuild proof"))
            contexts = ContextRepository(programs)
            compiler = ContextCompiler(contexts)
            compiled = compiler.compile(program.program_id, budget_units=100_000)
            receipt_id = compiled.receipt.context_receipt_id
            expected_receipt = compiled.receipt
            expected_context = canonical_json(compiled.context)

            programs._db.execute("DELETE FROM context_receipt_event_index")
            programs._db.execute("DELETE FROM context_receipts")
            programs.close()

            programs = ProgramRepository(path)
            try:
                contexts = ContextRepository(programs)
                rebuilt = contexts.get(receipt_id)
                self.assertEqual(rebuilt.receipt, expected_receipt)
                self.assertEqual(canonical_json(rebuilt.context), expected_context)
            finally:
                programs.close()

    def test_bounded_recall_never_restores_current_host_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            programs, program, contexts, _ = self._base(directory)
            try:
                refs = tuple(
                    contexts.persist_source(
                        program.program_id,
                        priority=ContextPriority.HOST_CONTROL,
                        payload={"ordinal": ordinal, "directive": "historical only"},
                    )
                    for ordinal in range(3)
                )
                recalled = contexts.recall(
                    program.program_id,
                    refs,
                    max_items=1,
                    max_units=100_000,
                )
                self.assertIs(recalled.completeness, ContextCompleteness.TRUNCATED)
                self.assertEqual(len(recalled.included_refs), 1)
                self.assertEqual(len(recalled.excluded_refs), 2)
                item = recalled.items[0]
                self.assertIs(item.priority, ContextPriority.RECALLED_HISTORY)
                self.assertEqual(item.currentness, "historical")
                self.assertEqual(item.authority, "historical_advisory")
                self.assertTrue(item.historical)
            finally:
                programs.close()

    def test_current_evidence_is_exact_and_stale_evidence_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kernel.db"
            programs = ProgramRepository(path)
            try:
                program = programs.create(Program("p-1", 0, "evidence Context proof"))
                evidence = EvidenceRepository(programs)
                current = evidence.admit(
                    content=b"current exact evidence",
                    source_class="fixture_observation",
                    observed_at=OBSERVED,
                    provenance=("fixture:source", "admission:host"),
                    trust_class="observed",
                    currentness="current",
                )
                stale = evidence.admit(
                    content=b"old exact evidence",
                    source_class="fixture_observation",
                    observed_at=OBSERVED,
                    provenance=("fixture:source", "admission:host"),
                    trust_class="observed",
                    currentness="stale",
                )
                contexts = ContextRepository(programs, evidence)
                compiler = ContextCompiler(contexts, evidence=evidence)
                compiled = compiler.compile(
                    program.program_id,
                    budget_units=100_000,
                    evidence_refs=(current.evidence_id,),
                )
                self.assertIn(evidence_ref(current.evidence_id), compiled.receipt.included_refs)
                source_entries = compiled.context["sources"]
                evidence_entry = next(
                    entry
                    for entry in source_entries
                    if entry["source_ref"] == evidence_ref(current.evidence_id)
                )
                self.assertEqual(
                    evidence_entry["payload"]["evidence"]["evidence_id"],
                    current.evidence_id,
                )
                with self.assertRaises(ContextIncomplete):
                    compiler.compile(
                        program.program_id,
                        budget_units=100_000,
                        evidence_refs=(stale.evidence_id,),
                    )
            finally:
                programs.close()

    def test_context_receipt_binds_exact_evidence_shown_to_inference(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kernel.db"
            programs = ProgramRepository(path)
            try:
                program = programs.create(Program("p-1", 0, "receipted inference proof"))
                evidence = EvidenceRepository(programs)
                admitted = evidence.admit(
                    content=b"evidence shown to model",
                    source_class="fixture_observation",
                    observed_at=OBSERVED,
                    provenance=("fixture:source", "admission:host"),
                    trust_class="observed",
                    currentness="current",
                )
                contexts = ContextRepository(programs, evidence)
                compiler = ContextCompiler(contexts, evidence=evidence)
                compiled = compiler.compile(
                    program.program_id,
                    budget_units=100_000,
                    evidence_refs=(admitted.evidence_id,),
                )

                actors = ActorRepository(programs)
                actors.register(Actor("actor-1", 0, "worker", "binding-a"))
                provider = CapturingProvider()
                bindings = ModelBindingRegistry()
                bindings.register("binding-a", provider)
                host = BoundedInferenceHost(programs, actors, bindings, contexts)

                result = host.infer(
                    program_id=program.program_id,
                    actor_id="actor-1",
                    context_receipt=compiled.receipt,
                    context=compiled.context,
                )
                request = actors.request(result.receipt.attempt_id)
                self.assertEqual(request.context, compiled.context)
                self.assertEqual(
                    result.receipt.context_receipt_ref,
                    compiled.receipt.context_receipt_id,
                )
                self.assertIn(
                    evidence_ref(admitted.evidence_id),
                    compiled.receipt.included_refs,
                )

                tampered = to_canonical_data(compiled.context)
                tampered["extra"] = "forged"
                with self.assertRaises(IntegrityViolation):
                    host.infer(
                        program_id=program.program_id,
                        actor_id="actor-1",
                        context_receipt=compiled.receipt,
                        context=tampered,
                    )
                self.assertEqual(len(provider.requests), 1)
            finally:
                programs.close()

    def test_compiled_capability_snapshot_is_exactly_preserved_at_inference_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "kernel.db")
            try:
                program = programs.create(Program("p-1", 0, "Capability Context proof"))
                capabilities = CapabilityRepository(programs)
                handlers = CapabilityHandlerRegistry()
                install_builtin_capabilities(capabilities, handlers)
                snapshot = CapabilityBroker(capabilities, handlers).snapshot(
                    ("workspace.read", "artifact.write")
                )
                contexts = ContextRepository(programs)
                compiler = ContextCompiler(contexts, capabilities=capabilities)
                compiled = compiler.compile(
                    program.program_id,
                    budget_units=100_000,
                    capability_snapshot=snapshot,
                )

                actors = ActorRepository(programs)
                actors.register(Actor("actor-1", 0, "worker", "binding-a"))
                provider = CapturingProvider()
                bindings = ModelBindingRegistry()
                bindings.register("binding-a", provider)
                host = BoundedInferenceHost(
                    programs,
                    actors,
                    bindings,
                    contexts,
                    capabilities,
                )
                result = host.infer(
                    program_id=program.program_id,
                    actor_id="actor-1",
                    context_receipt=compiled.receipt,
                    context=compiled.context,
                    capability_snapshot=snapshot,
                )
                request = actors.request(result.receipt.attempt_id)
                self.assertEqual(request.context, compiled.context)
                self.assertEqual(
                    canonical_json(request.context["capability_snapshot"]),
                    canonical_json(to_canonical_data(snapshot)),
                )
            finally:
                programs.close()

    def test_explicit_incomplete_coverage_is_receipted(self):
        with tempfile.TemporaryDirectory() as directory:
            programs, program, _, compiler = self._base(directory)
            try:
                compiled = compiler.compile(
                    program.program_id,
                    budget_units=100_000,
                    coverage_complete=False,
                )
                self.assertIs(
                    compiled.receipt.completeness,
                    ContextCompleteness.INCOMPLETE,
                )
            finally:
                programs.close()


if __name__ == "__main__":
    unittest.main()
