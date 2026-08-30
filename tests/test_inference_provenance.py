from dataclasses import replace
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_capital.kernel.actor_store import ActorRepository
from ai_capital.kernel.deterministic_provider import DeterministicInferenceProvider
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ContextCompleteness, ModelAttemptOutcome
from ai_capital.kernel.errors import IntegrityViolation
from ai_capital.kernel.inference import InferenceHost, ModelBindingRegistry
from ai_capital.kernel.models import (
    Actor,
    ContextReceipt,
    InferenceRequest,
    ModelAttemptReceipt,
    ModelTurn,
    Program,
)
from ai_capital.kernel.schema_codec import record_from_json, record_to_json
from ai_capital.kernel.serialization import canonical_digest


def receipt(program: Program) -> ContextReceipt:
    return ContextReceipt(
        "ctx-1",
        program.program_id,
        program.revision,
        (f"program:{program.program_id}",),
        (),
        ContextCompleteness.COMPLETE,
        100,
        "2026-08-30T00:00:00Z",
    )


def successful_attempt(path: Path):
    programs = ProgramRepository(path)
    program = programs.create(Program("p-1", 0, "provenance"))
    actors = ActorRepository(programs)
    actors.register(Actor("actor-1", 0, "worker", "binding-a"))
    bindings = ModelBindingRegistry()
    bindings.register("binding-a", DeterministicInferenceProvider("result"))
    result = InferenceHost(programs, actors, bindings).infer(
        program_id="p-1",
        actor_id="actor-1",
        context_receipt=receipt(program),
        context={"objective": program.objective, "revision": program.revision},
    )
    return programs, actors, result


class InferenceProvenanceTests(unittest.TestCase):
    def test_inference_request_and_attempt_receipt_round_trip(self):
        context_receipt = ContextReceipt(
            "ctx-1",
            "p-1",
            5,
            ("program:p-1", "evidence:e-1"),
            ("history:h-1",),
            ContextCompleteness.TRUNCATED,
            100,
            "2026-08-30T00:00:00Z",
        )
        request = InferenceRequest(
            "attempt-1",
            "actor-1",
            2,
            "p-1",
            5,
            "binding-a",
            context_receipt,
            {"objective": "bounded"},
        )
        attempt = ModelAttemptReceipt(
            "attempt-1",
            "actor-1",
            2,
            "p-1",
            5,
            "binding-a",
            "ctx-1",
            canonical_digest(request),
            "config-digest",
            ModelAttemptOutcome.SUCCEEDED,
            "2026-08-30T00:00:00Z",
            "2026-08-30T00:00:01Z",
            canonical_digest(ModelTurn(provenance_receipt="attempt-1")),
            None,
        )
        restored_request = record_from_json(InferenceRequest, record_to_json(request))
        self.assertEqual(restored_request, request)
        self.assertEqual(restored_request.context_receipt, context_receipt)
        self.assertEqual(restored_request.context_receipt_ref, "ctx-1")
        self.assertEqual(record_from_json(ModelAttemptReceipt, record_to_json(attempt)), attempt)

    def test_inference_request_rejects_inconsistent_context_receipt(self):
        wrong_program = ContextReceipt(
            "ctx-1", "other", 5, (), (), ContextCompleteness.COMPLETE, 10,
            "2026-08-30T00:00:00Z",
        )
        with self.assertRaises(ValueError):
            InferenceRequest(
                "attempt-1", "actor-1", 0, "p-1", 5, "binding-a",
                wrong_program, {"objective": "bounded"},
            )

        wrong_revision = replace(wrong_program, program_id="p-1", program_revision=4)
        with self.assertRaises(ValueError):
            InferenceRequest(
                "attempt-1", "actor-1", 0, "p-1", 5, "binding-a",
                wrong_revision, {"objective": "bounded"},
            )

    def test_exact_inference_request_is_durable_and_digest_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kernel.db"
            programs, actors, result = successful_attempt(path)
            try:
                stored = actors.request(result.receipt.attempt_id)
                self.assertEqual(stored.context["objective"], "provenance")
                self.assertEqual(stored.context["revision"], 0)
                self.assertEqual(stored.context_receipt.context_receipt_id, "ctx-1")
                self.assertEqual(stored.context_receipt.program_id, "p-1")
                self.assertEqual(stored.context_receipt.program_revision, 0)
                self.assertEqual(result.receipt.input_digest, canonical_digest(stored))
            finally:
                programs.close()

            with ProgramRepository(path) as restarted:
                actors = ActorRepository(restarted)
                stored = actors.request(result.receipt.attempt_id)
                self.assertEqual(stored.context_receipt.context_receipt_id, "ctx-1")
                self.assertEqual(result.receipt.input_digest, canonical_digest(stored))

    def test_redundant_attempt_row_tampering_is_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kernel.db"
            programs, actors, result = successful_attempt(path)
            programs.close()
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "UPDATE model_attempts SET program_revision = 999 WHERE attempt_id = ?",
                    (result.receipt.attempt_id,),
                )
                connection.commit()
            finally:
                connection.close()

            with ProgramRepository(path) as restarted:
                actors = ActorRepository(restarted)
                with self.assertRaises(IntegrityViolation):
                    actors.attempts("actor-1")

    def test_request_tampering_is_detected_even_when_receipt_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kernel.db"
            programs, actors, result = successful_attempt(path)
            original = actors.request(result.receipt.attempt_id)
            programs.close()

            changed = replace(original, context={"objective": "tampered", "revision": 0})
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "UPDATE model_attempts SET request_json = ? WHERE attempt_id = ?",
                    (record_to_json(changed), result.receipt.attempt_id),
                )
                connection.commit()
            finally:
                connection.close()

            with ProgramRepository(path) as restarted:
                actors = ActorRepository(restarted)
                with self.assertRaises(IntegrityViolation):
                    actors.request(result.receipt.attempt_id)

    def test_context_receipt_tampering_is_detected_even_when_context_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kernel.db"
            programs, actors, result = successful_attempt(path)
            original = actors.request(result.receipt.attempt_id)
            programs.close()

            changed_receipt = replace(original.context_receipt, budget_units=999)
            changed = replace(original, context_receipt=changed_receipt)
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "UPDATE model_attempts SET request_json = ? WHERE attempt_id = ?",
                    (record_to_json(changed), result.receipt.attempt_id),
                )
                connection.commit()
            finally:
                connection.close()

            with ProgramRepository(path) as restarted:
                actors = ActorRepository(restarted)
                with self.assertRaises(IntegrityViolation):
                    actors.request(result.receipt.attempt_id)

    def test_output_provenance_tampering_is_detected_even_with_recomputed_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kernel.db"
            programs, actors, result = successful_attempt(path)
            original = actors.turn(result.receipt.attempt_id)
            programs.close()
            changed = replace(original, provenance_receipt="different-attempt")
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "UPDATE model_attempts SET turn_json = ?, turn_digest = ? WHERE attempt_id = ?",
                    (
                        record_to_json(changed),
                        canonical_digest(changed),
                        result.receipt.attempt_id,
                    ),
                )
                connection.commit()
            finally:
                connection.close()

            with ProgramRepository(path) as restarted:
                actors = ActorRepository(restarted)
                with self.assertRaises(IntegrityViolation):
                    actors.turn(result.receipt.attempt_id)


if __name__ == "__main__":
    unittest.main()
