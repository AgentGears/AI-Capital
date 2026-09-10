from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import ai_capital.kernel.context as context_module
from ai_capital.kernel.actor_store import ActorRepository
from ai_capital.kernel.bounded_inference import BoundedInferenceHost
from ai_capital.kernel.context import ContextCompiler, ContextRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ContextPriority, ModelAttemptOutcome
from ai_capital.kernel.errors import IntegrityViolation
from ai_capital.kernel.evidence_store import EvidenceRepository
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


class K8ReviewRound43Tests(unittest.TestCase):
    def test_success_freshness_and_attempt_commit_share_one_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "atomic inference freshness"))
                contexts = ContextRepository(programs)
                compiler = ContextCompiler(contexts)
                actors = ActorRepository(programs)
                actors.register(Actor("actor-1", 0, "worker", "binding-a"))
                provider = CapturingProvider()
                bindings = ModelBindingRegistry()
                bindings.register("binding-a", provider)
                host = BoundedInferenceHost(programs, actors, bindings, contexts)
                compiled = compiler.compile(program.program_id, budget_units=100_000)

                original_record_attempt = actors.record_attempt
                injected = False

                def record_attempt_with_late_control(receipt, turn, request, **kwargs):
                    nonlocal injected
                    if receipt.outcome is ModelAttemptOutcome.SUCCEEDED and not injected:
                        injected = True
                        contexts.persist_source(
                            program.program_id,
                            priority=ContextPriority.HOST_CONTROL,
                            payload={"control": "arrived before success transaction"},
                        )
                    return original_record_attempt(receipt, turn, request, **kwargs)

                with patch.object(
                    actors,
                    "record_attempt",
                    side_effect=record_attempt_with_late_control,
                ):
                    with self.assertRaisesRegex(
                        IntegrityViolation,
                        "stale for current inference Context",
                    ):
                        host.infer(
                            program_id=program.program_id,
                            actor_id="actor-1",
                            context_receipt=compiled.receipt,
                            context=compiled.context,
                        )

                self.assertTrue(injected)
                self.assertEqual(len(provider.requests), 1)
                attempts = actors.attempts("actor-1")
                self.assertEqual(len(attempts), 1)
                self.assertIs(attempts[0].outcome, ModelAttemptOutcome.STALE)
                self.assertEqual(attempts[0].error_code, "stale_inference_context")
            finally:
                programs.close()

    def test_same_length_artifact_corruption_fails_before_current_budget_exclusion(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "current Evidence integrity"))
                evidence = EvidenceRepository(programs)
                item = evidence.admit(
                    content=b"trusted-evidence",
                    source_class="test",
                    observed_at="2026-01-01T00:00:00Z",
                    provenance=("test",),
                    trust_class="test",
                    currentness="current",
                )
                contexts = ContextRepository(programs, evidence)
                compiler = ContextCompiler(contexts, evidence=evidence)
                baseline = compiler.compile(program.program_id, budget_units=100_000)
                evidence._artifact_path(item.digest).write_bytes(b"x" * len(b"trusted-evidence"))

                with patch.object(
                    evidence,
                    "_read_artifact",
                    side_effect=AssertionError("corrupt excluded artifact was materialized"),
                ) as read_artifact:
                    with self.assertRaisesRegex(IntegrityViolation, "artifact digest mismatch"):
                        compiler.compile(
                            program.program_id,
                            budget_units=baseline.used_units,
                            evidence_refs=(item.evidence_id,),
                        )
                read_artifact.assert_not_called()
            finally:
                programs.close()

    def test_same_length_artifact_corruption_fails_before_recall_budget_exclusion(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "historical Evidence integrity"))
                evidence = EvidenceRepository(programs)
                item = evidence.admit(
                    content=b"trusted-history",
                    source_class="test",
                    observed_at="2026-01-01T00:00:00Z",
                    provenance=("test",),
                    trust_class="test",
                    currentness="historical",
                )
                contexts = ContextRepository(programs, evidence)
                evidence._artifact_path(item.digest).write_bytes(b"y" * len(b"trusted-history"))
                empty_budget = context_module._canonical_units({"sources": []})

                with patch.object(
                    contexts,
                    "_resolve_recall",
                    side_effect=AssertionError("corrupt excluded Evidence was materialized"),
                ) as resolve:
                    with self.assertRaisesRegex(IntegrityViolation, "artifact digest mismatch"):
                        contexts.recall(
                            program.program_id,
                            (f"evidence:{item.evidence_id}",),
                            max_items=1,
                            max_units=empty_budget,
                        )
                resolve.assert_not_called()
            finally:
                programs.close()

    def test_host_control_projection_index_keys_current_revision_before_event_order(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                programs.create(Program("p-1", 0, "revision-scoped Host-control index"))
                ContextRepository(programs)
                columns = tuple(
                    str(row["name"])
                    for row in programs._db.execute(
                        "PRAGMA index_info(context_persisted_source_program_revision_priority)"
                    ).fetchall()
                )
                self.assertEqual(
                    columns,
                    ("program_id", "program_revision", "priority", "event_id"),
                )
                plan = " ".join(
                    str(row[3])
                    for row in programs._db.execute(
                        """
                        EXPLAIN QUERY PLAN
                        SELECT sequence, event_id, program_id, program_revision, priority,
                               source_digest, payload_units, event_digest, projection_digest
                        FROM context_persisted_source_index
                        WHERE program_id = ? AND program_revision = ? AND priority = ?
                        ORDER BY event_id
                        """,
                        ("p-1", 0, ContextPriority.HOST_CONTROL.value),
                    ).fetchall()
                )
                self.assertIn(
                    "context_persisted_source_program_revision_priority",
                    plan,
                )
            finally:
                programs.close()


if __name__ == "__main__":
    unittest.main()
