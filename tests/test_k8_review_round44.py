from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from ai_capital.kernel.actor_store import ActorRepository
from ai_capital.kernel.bounded_inference import BoundedInferenceHost
from ai_capital.kernel.context import ContextCompiler, ContextRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ModelAttemptOutcome
from ai_capital.kernel.errors import IntegrityViolation
from ai_capital.kernel.inference import ModelBindingRegistry
from ai_capital.kernel.models import Actor, ModelTurn, Program


class MutatingProvider:
    def __init__(self):
        self.requests = []
        self.mutate = lambda request: None

    def effective_configuration(self):
        return {"kind": "mutating", "revision": 1}

    def generate(self, request):
        self.requests.append(request)
        self.mutate(request)
        return ModelTurn(provenance_receipt=request.attempt_id)


class K8ReviewRound44Tests(unittest.TestCase):
    def test_receipt_integrity_is_revalidated_inside_success_transaction(self):
        for mutation in ("receipt_projection", "compiled_event"):
            with self.subTest(mutation=mutation):
                with tempfile.TemporaryDirectory() as directory:
                    programs = ProgramRepository(Path(directory) / "host.db")
                    try:
                        program = programs.create(
                            Program("p-1", 0, "atomic compiled Context integrity")
                        )
                        contexts = ContextRepository(programs)
                        compiler = ContextCompiler(contexts)
                        actors = ActorRepository(programs)
                        actors.register(Actor("actor-1", 0, "worker", "binding-a"))
                        provider = MutatingProvider()
                        bindings = ModelBindingRegistry()
                        bindings.register("binding-a", provider)
                        host = BoundedInferenceHost(
                            programs,
                            actors,
                            bindings,
                            contexts,
                        )
                        compiled = compiler.compile(
                            program.program_id,
                            budget_units=100_000,
                        )

                        if mutation == "receipt_projection":
                            def mutate(_request):
                                programs._db.execute(
                                    """
                                    UPDATE context_receipts
                                    SET context_json = context_json || ' '
                                    WHERE context_receipt_id = ?
                                    """,
                                    (compiled.receipt.context_receipt_id,),
                                )
                        else:
                            compiled_event_id = programs._db.execute(
                                """
                                SELECT compiled_event_id
                                FROM context_receipts
                                WHERE context_receipt_id = ?
                                """,
                                (compiled.receipt.context_receipt_id,),
                            ).fetchone()[0]

                            def mutate(_request):
                                programs._db.execute(
                                    """
                                    UPDATE events
                                    SET event_json = event_json || ' '
                                    WHERE event_id = ?
                                    """,
                                    (compiled_event_id,),
                                )

                        provider.mutate = mutate

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

                        self.assertEqual(len(provider.requests), 1)
                        attempts = actors.attempts("actor-1")
                        self.assertEqual(len(attempts), 1)
                        self.assertIs(attempts[0].outcome, ModelAttemptOutcome.STALE)
                        self.assertEqual(
                            attempts[0].error_code,
                            "stale_inference_context",
                        )
                        with self.assertRaises(IntegrityViolation):
                            contexts.get(compiled.receipt.context_receipt_id)
                    finally:
                        programs.close()


if __name__ == "__main__":
    unittest.main()
