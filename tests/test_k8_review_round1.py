from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_capital.kernel.actor_store import ActorRepository
from ai_capital.kernel.bounded_inference import BoundedInferenceHost
from ai_capital.kernel.capability_store import CapabilityRepository
from ai_capital.kernel.context import ContextRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ContextPriority
from ai_capital.kernel.errors import InvalidRequest
from ai_capital.kernel.evidence_store import EvidenceRepository
from ai_capital.kernel.inference import ModelBindingRegistry
from ai_capital.kernel.models import Program


class K8ReviewRound1Tests(unittest.TestCase):
    def test_bounded_inference_rejects_actor_repository_from_another_host(self):
        with tempfile.TemporaryDirectory() as directory:
            host_a = ProgramRepository(Path(directory) / "host-a.db")
            host_b = ProgramRepository(Path(directory) / "host-b.db")
            try:
                contexts = ContextRepository(host_a)
                actors = ActorRepository(host_b)
                with self.assertRaises(InvalidRequest):
                    BoundedInferenceHost(
                        host_a,
                        actors,
                        ModelBindingRegistry(),
                        contexts,
                    )
            finally:
                host_a.close()
                host_b.close()

    def test_bounded_inference_rejects_capability_repository_from_another_host(self):
        with tempfile.TemporaryDirectory() as directory:
            host_a = ProgramRepository(Path(directory) / "host-a.db")
            host_b = ProgramRepository(Path(directory) / "host-b.db")
            try:
                contexts = ContextRepository(host_a)
                actors = ActorRepository(host_a)
                capabilities = CapabilityRepository(host_b)
                with self.assertRaises(InvalidRequest):
                    BoundedInferenceHost(
                        host_a,
                        actors,
                        ModelBindingRegistry(),
                        contexts,
                        capabilities,
                    )
            finally:
                host_a.close()
                host_b.close()

    def test_context_repository_rejects_evidence_repository_from_another_host(self):
        with tempfile.TemporaryDirectory() as directory:
            host_a = ProgramRepository(Path(directory) / "host-a.db")
            host_b = ProgramRepository(Path(directory) / "host-b.db")
            try:
                foreign_evidence = EvidenceRepository(host_b)
                with self.assertRaises(InvalidRequest):
                    ContextRepository(host_a, foreign_evidence)
            finally:
                host_a.close()
                host_b.close()

    def test_bounded_recall_selects_by_stable_reference_before_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            host = ProgramRepository(Path(directory) / "host.db")
            try:
                program = host.create(Program("p-1", 0, "stable recall selection"))
                contexts = ContextRepository(host)
                first = contexts.persist_source(
                    program.program_id,
                    priority=ContextPriority.ADVISORY_MEMORY,
                    payload={"ordinal": 1, "text": "a" * 64},
                )
                second = contexts.persist_source(
                    program.program_id,
                    priority=ContextPriority.ADVISORY_MEMORY,
                    payload={"ordinal": 2, "text": "b" * 64},
                )

                forward = contexts.recall(
                    program.program_id,
                    (first, second),
                    max_items=1,
                    max_units=100_000,
                )
                reverse = contexts.recall(
                    program.program_id,
                    (second, first),
                    max_items=1,
                    max_units=100_000,
                )

                self.assertEqual(forward.requested_refs, reverse.requested_refs)
                self.assertEqual(forward.included_refs, reverse.included_refs)
                self.assertEqual(forward.excluded_refs, reverse.excluded_refs)
                self.assertEqual(forward.items, reverse.items)
            finally:
                host.close()


if __name__ == "__main__":
    unittest.main()
