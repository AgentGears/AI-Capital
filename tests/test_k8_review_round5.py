from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_capital.kernel.context import ContextRepository, evidence_ref
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ContextCompleteness, ContextPriority
from ai_capital.kernel.errors import InvalidRequest
from ai_capital.kernel.evidence_store import EvidenceRepository
from ai_capital.kernel.models import Program


OBSERVED = "2026-09-03T00:00:00Z"


class K8ReviewRound5Tests(unittest.TestCase):
    def test_byte_budget_rejection_still_bounds_materialization_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "bounded recall byte budget"))
                evidence = EvidenceRepository(programs)
                admitted = tuple(
                    evidence.admit(
                        content=(f"large-evidence-{index}".encode("utf-8") * 4096),
                        source_class="fixture_observation",
                        observed_at=OBSERVED,
                        provenance=("fixture:source", "admission:host"),
                        trust_class="observed",
                        currentness="current",
                    )
                    for index in range(3)
                )
                contexts = ContextRepository(programs, evidence)
                requested = tuple(evidence_ref(item.evidence_id) for item in admitted)

                with patch.object(
                    contexts,
                    "_resolve_recall",
                    wraps=contexts._resolve_recall,
                ) as resolve, patch.object(
                    evidence,
                    "artifact",
                    wraps=evidence.artifact,
                ) as artifact:
                    result = contexts.recall(
                        program.program_id,
                        requested,
                        max_items=1,
                        max_units=128,
                    )

                self.assertEqual(resolve.call_count, 1)
                self.assertEqual(artifact.call_count, 1)
                self.assertEqual(result.included_refs, ())
                self.assertEqual(result.excluded_refs, tuple(sorted(requested)))
                self.assertIs(result.completeness, ContextCompleteness.TRUNCATED)
                self.assertLessEqual(result.used_units, result.budget_units)
            finally:
                programs.close()

    def test_skipped_unsupported_address_is_validated_before_materialization(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "recall address validation"))
                contexts = ContextRepository(programs)
                valid_ref = contexts.persist_source(
                    program.program_id,
                    priority=ContextPriority.ADVISORY_MEMORY,
                    payload={"memory": "durable"},
                )
                with patch.object(
                    contexts,
                    "_resolve_recall",
                    wraps=contexts._resolve_recall,
                ) as resolve:
                    with self.assertRaises(InvalidRequest):
                        contexts.recall(
                            program.program_id,
                            (valid_ref, "nonsense"),
                            max_items=1,
                            max_units=100_000,
                        )
                self.assertEqual(resolve.call_count, 0)
            finally:
                programs.close()

    def test_skipped_unknown_address_is_validated_before_materialization(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "recall address validation"))
                contexts = ContextRepository(programs)
                valid_ref = contexts.persist_source(
                    program.program_id,
                    priority=ContextPriority.ADVISORY_MEMORY,
                    payload={"memory": "durable"},
                )
                with patch.object(
                    contexts,
                    "_resolve_recall",
                    wraps=contexts._resolve_recall,
                ) as resolve:
                    with self.assertRaises(InvalidRequest):
                        contexts.recall(
                            program.program_id,
                            (valid_ref, "event:zzzz"),
                            max_items=1,
                            max_units=100_000,
                        )
                self.assertEqual(resolve.call_count, 0)
            finally:
                programs.close()

    def test_skipped_cross_program_address_is_validated_before_materialization(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                first = programs.create(Program("p-1", 0, "first Program"))
                second = programs.create(Program("p-2", 0, "second Program"))
                contexts = ContextRepository(programs)
                first_ref = contexts.persist_source(
                    first.program_id,
                    priority=ContextPriority.ADVISORY_MEMORY,
                    payload={"memory": "first"},
                )
                second_ref = contexts.persist_source(
                    second.program_id,
                    priority=ContextPriority.ADVISORY_MEMORY,
                    payload={"memory": "second"},
                )
                with patch.object(
                    contexts,
                    "_resolve_recall",
                    wraps=contexts._resolve_recall,
                ) as resolve:
                    with self.assertRaises(InvalidRequest):
                        contexts.recall(
                            first.program_id,
                            (first_ref, second_ref),
                            max_items=1,
                            max_units=100_000,
                        )
                self.assertEqual(resolve.call_count, 0)
            finally:
                programs.close()


if __name__ == "__main__":
    unittest.main()
