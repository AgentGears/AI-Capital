from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import ai_capital.kernel.context as context_module
from ai_capital.kernel.actor_store import ActorRepository
from ai_capital.kernel.bounded_inference import BoundedInferenceHost
from ai_capital.kernel.capability_store import CapabilityRepository, capability_descriptor
from ai_capital.kernel.context import ContextCompiler, ContextRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import (
    ContextPriority,
    EffectClass,
    ModelAttemptOutcome,
    Reversibility,
    RiskClass,
)
from ai_capital.kernel.errors import ContextBudgetExceeded, IntegrityViolation, InvalidRequest
from ai_capital.kernel.inference import ModelBindingRegistry
from ai_capital.kernel.models import Actor, Capability, ModelTurn, Program
from ai_capital.kernel.schema_codec import record_to_json
from ai_capital.kernel.serialization import canonical_digest


class CapturingProvider:
    def __init__(self):
        self.requests = []

    def effective_configuration(self):
        return {"kind": "capture", "revision": 1}

    def generate(self, request):
        self.requests.append(request)
        return ModelTurn(provenance_receipt=request.attempt_id)


class HostControlMutatingProvider(CapturingProvider):
    def __init__(self, contexts: ContextRepository, program_id: str):
        super().__init__()
        self._contexts = contexts
        self._program_id = program_id

    def generate(self, request):
        self.requests.append(request)
        self._contexts.persist_source(
            self._program_id,
            priority=ContextPriority.HOST_CONTROL,
            payload={"control": "arrived during inference"},
        )
        return ModelTurn(provenance_receipt=request.attempt_id)


class K8ReviewRound27Tests(unittest.TestCase):
    def _inference_host(self, directory: str, provider):
        programs = ProgramRepository(Path(directory) / "host.db")
        program = programs.create(Program("p-1", 0, "bounded Host-control freshness"))
        contexts = ContextRepository(programs)
        compiler = ContextCompiler(contexts)
        actors = ActorRepository(programs)
        actors.register(Actor("actor-1", 0, "worker", "binding-a"))
        bindings = ModelBindingRegistry()
        bindings.register("binding-a", provider(contexts, program.program_id) if callable(provider) else provider)
        host = BoundedInferenceHost(programs, actors, bindings, contexts)
        return programs, program, contexts, compiler, actors, bindings.resolve("binding-a"), host

    def test_host_control_change_during_provider_is_recorded_stale(self):
        with tempfile.TemporaryDirectory() as directory:
            programs, program, contexts, compiler, actors, provider, host = self._inference_host(
                directory, HostControlMutatingProvider
            )
            try:
                compiled = compiler.compile(program.program_id, budget_units=100_000)
                with self.assertRaises(IntegrityViolation):
                    host.infer(
                        program_id=program.program_id,
                        actor_id="actor-1",
                        context_receipt=compiled.receipt,
                        context=compiled.context,
                    )
                self.assertEqual(len(provider.requests), 1)
                attempts = actors.attempts("actor-1")
                self.assertEqual(len(attempts), 1)
                self.assertIs(attempts[0].outcome, ModelAttemptOutcome.STALE)
                self.assertEqual(attempts[0].error_code, "stale_inference_context")
            finally:
                programs.close()

    def test_compilation_bounds_host_control_enumeration_by_context_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "bounded control enumeration"))
                contexts = ContextRepository(programs)
                compiler = ContextCompiler(contexts)
                baseline = compiler.compile(program.program_id, budget_units=100_000)
                max_refs = baseline.used_units // context_module._MIN_HOST_CONTROL_SOURCE_UNITS
                for index in range(max_refs + 3):
                    contexts.persist_source(
                        program.program_id,
                        priority=ContextPriority.HOST_CONTROL,
                        payload={"control": index},
                    )
                digest_fn = context_module._persisted_source_event_metadata_digest
                with patch(
                    "ai_capital.kernel.context._persisted_source_event_metadata_digest",
                    wraps=digest_fn,
                ) as digest:
                    with self.assertRaises(ContextBudgetExceeded):
                        compiler.compile(
                            program.program_id,
                            budget_units=baseline.used_units,
                        )
                self.assertLessEqual(digest.call_count, max_refs)
            finally:
                programs.close()

    def test_inference_bounds_host_control_enumeration_by_receipt_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = CapturingProvider()
            programs, program, contexts, compiler, actors, provider, host = self._inference_host(
                directory, provider
            )
            try:
                compiled = compiler.compile(program.program_id, budget_units=100_000)
                receipt_capacity = len(set(compiled.receipt.included_refs))
                for index in range(receipt_capacity + 4):
                    contexts.persist_source(
                        program.program_id,
                        priority=ContextPriority.HOST_CONTROL,
                        payload={"control": index},
                    )
                digest_fn = context_module._persisted_source_event_metadata_digest
                with patch(
                    "ai_capital.kernel.context._persisted_source_event_metadata_digest",
                    wraps=digest_fn,
                ) as digest:
                    with self.assertRaises(IntegrityViolation):
                        host.infer(
                            program_id=program.program_id,
                            actor_id="actor-1",
                            context_receipt=compiled.receipt,
                            context=compiled.context,
                        )
                self.assertLessEqual(digest.call_count, receipt_capacity)
                self.assertEqual(provider.requests, [])
                self.assertEqual(actors.attempts("actor-1"), ())
            finally:
                programs.close()

    def test_v1_oversized_capability_remains_readable_and_repairable_after_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                capabilities = CapabilityRepository(programs)
                current = capabilities.register(
                    Capability(
                        capability_id="capability.legacy-binding",
                        schema_version=1,
                        operation="observe",
                        resource_type="artifact",
                        effect_class=EffectClass.OBSERVE,
                        reversibility=Reversibility.REVERSIBLE,
                        risk_class=RiskClass.LOW,
                        input_schema={"type": "object", "properties": {}, "required": (), "additional_properties": False},
                        output_schema={"type": "object", "properties": {}, "required": (), "additional_properties": False},
                        binding_revision=0,
                        handler_binding="bounded-handler",
                    )
                )
                snapshot = capabilities.create_snapshot((capability_descriptor(current),))
                legacy = replace(current, handler_binding="h" * 8192)
                encoded = record_to_json(legacy)
                digest = canonical_digest(legacy)
                with programs._transaction():
                    programs._db.execute(
                        "UPDATE capability_bindings SET capability_json = ?, capability_digest = ? WHERE capability_id = ? AND binding_revision = 0",
                        (encoded, digest, legacy.capability_id),
                    )
                    programs._db.execute(
                        "UPDATE capability_projections SET capability_json = ?, capability_digest = ? WHERE capability_id = ?",
                        (encoded, digest, legacy.capability_id),
                    )
                    programs._db.execute(
                        "UPDATE component_schema SET version = 1 WHERE component = 'capability_registry'"
                    )

                migrated = CapabilityRepository(programs)
                version = programs._db.execute(
                    "SELECT version FROM component_schema WHERE component = 'capability_registry'"
                ).fetchone()[0]
                self.assertEqual(int(version), 2)
                self.assertEqual(migrated.get(legacy.capability_id), legacy)
                self.assertEqual(migrated.get_snapshot(snapshot.snapshot_id), snapshot)

                repaired = migrated.replace_handler(
                    legacy.capability_id,
                    "repaired-handler",
                    expected_binding_revision=0,
                )
                self.assertEqual(repaired.binding_revision, 1)
                self.assertEqual(repaired.handler_binding, "repaired-handler")
                self.assertEqual(migrated.bindings(legacy.capability_id)[0], legacy)
            finally:
                programs.close()


if __name__ == "__main__":
    unittest.main()
