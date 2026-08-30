from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import sys
import tempfile
import threading
import unittest
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_capital.kernel.actor_store import ActorRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ContextCompleteness, ModelAttemptOutcome
from ai_capital.kernel.errors import InternalFault
from ai_capital.kernel.inference import InferenceHost, ModelBindingRegistry
from ai_capital.kernel.json_http_provider import JsonHttpInferenceProvider
from ai_capital.kernel.models import Actor, ContextReceipt, Program
from ai_capital.kernel.serialization import canonical_digest


class ResponseHandler(BaseHTTPRequestHandler):
    response_body = {}
    seen_request = None

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        type(self).seen_request = json.loads(body)
        payload = json.dumps(type(self).response_body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        return


def serve(response_body):
    ResponseHandler.response_body = response_body
    ResponseHandler.seen_request = None
    server = ThreadingHTTPServer(("127.0.0.1", 0), ResponseHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_port}/infer"


def context_receipt(program: Program) -> ContextReceipt:
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


class JsonHttpProviderTests(unittest.TestCase):
    def test_generic_http_provider_round_trip_preserves_host_identity(self):
        response = {
            "reasoning_proposals": ["inspect the objective"],
            "capability_requests": [
                {
                    "capability_id": "workspace.read",
                    "arguments": {"path": "input.txt"},
                    "expected_binding_revision": 2,
                }
            ],
            "claim_proposals": [
                {"statement": "input exists", "evidence_refs": ["e-1"]}
            ],
            "completion_proposal": {"rationale": "bounded work appears complete"},
        }
        server, thread, endpoint = serve(response)
        try:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "kernel.db"
                with ProgramRepository(path) as programs:
                    program = programs.create(Program("p-1", 0, "HTTP provider test"))
                    actors = ActorRepository(programs)
                    actors.register(Actor("actor-1", 0, "worker", "binding-http"))
                    bindings = ModelBindingRegistry()
                    bindings.register(
                        "binding-http",
                        JsonHttpInferenceProvider(endpoint, "profile-a", timeout_seconds=2),
                    )
                    result = InferenceHost(programs, actors, bindings).infer(
                        program_id="p-1",
                        actor_id="actor-1",
                        context_receipt=context_receipt(program),
                        context={"objective": program.objective},
                    )

                    seen = ResponseHandler.seen_request
                    self.assertIsNotNone(seen)
                    self.assertEqual(seen["attempt_id"], result.receipt.attempt_id)
                    self.assertEqual(seen["context_receipt"]["context_receipt_id"], "ctx-1")
                    self.assertEqual(seen["context_receipt"]["program_id"], "p-1")
                    self.assertEqual(seen["context_receipt"]["program_revision"], 0)
                    UUID(result.receipt.attempt_id)
                    self.assertEqual(result.turn.provenance_receipt, result.receipt.attempt_id)
                    self.assertEqual(result.turn.reasoning_proposals[0].text, "inspect the objective")
                    self.assertEqual(result.turn.capability_requests[0].capability_id, "workspace.read")
                    UUID(result.turn.capability_requests[0].request_id)
                    self.assertEqual(result.turn.claim_proposals[0].statement, "input exists")
                    self.assertEqual(
                        result.turn.completion_proposal.rationale,
                        "bounded work appears complete",
                    )
                    stored_request = actors.request(result.receipt.attempt_id)
                    self.assertEqual(stored_request.context["objective"], program.objective)
                    self.assertEqual(stored_request.context_receipt.context_receipt_id, "ctx-1")
                    self.assertEqual(result.receipt.input_digest, canonical_digest(stored_request))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_unknown_remote_identity_field_fails_closed_and_is_receipted(self):
        server, thread, endpoint = serve({
            "reasoning_proposals": ["text"],
            "remote_call_id": "remote-controlled-identity",
        })
        try:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "kernel.db"
                with ProgramRepository(path) as programs:
                    program = programs.create(Program("p-1", 0, "reject remote identity"))
                    actors = ActorRepository(programs)
                    actors.register(Actor("actor-1", 0, "worker", "binding-http"))
                    bindings = ModelBindingRegistry()
                    bindings.register(
                        "binding-http",
                        JsonHttpInferenceProvider(endpoint, "profile-a", timeout_seconds=2),
                    )
                    with self.assertRaises(InternalFault):
                        InferenceHost(programs, actors, bindings).infer(
                            program_id="p-1",
                            actor_id="actor-1",
                            context_receipt=context_receipt(program),
                            context={"objective": program.objective},
                        )
                    receipt = actors.attempts("actor-1")[-1]
                    self.assertEqual(receipt.outcome, ModelAttemptOutcome.FAILED)
                    self.assertEqual(receipt.error_code, "provider_failure")
                    UUID(receipt.attempt_id)
                    self.assertNotEqual(receipt.attempt_id, "remote-controlled-identity")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
