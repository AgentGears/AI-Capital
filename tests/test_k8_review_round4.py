from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_capital.kernel.context import ContextRepository, evidence_ref
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ContextCompleteness
from ai_capital.kernel.evidence_store import EvidenceRepository
from ai_capital.kernel.models import Program


OBSERVED = "2026-09-03T00:00:00Z"


class K8ReviewRound4Tests(unittest.TestCase):
    def test_item_limit_stops_evidence_materialization_before_resolve(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "bounded recall materialization"))
                evidence = EvidenceRepository(programs)
                admitted = tuple(
                    evidence.admit(
                        content=(f"evidence-{index}".encode("utf-8") * 1024),
                        source_class="fixture_observation",
                        observed_at=OBSERVED,
                        provenance=("fixture:source", "admission:host"),
                        trust_class="observed",
                        currentness="current",
                    )
                    for index in range(3)
                )
                contexts = ContextRepository(programs, evidence)
                requested = tuple(
                    evidence_ref(item.evidence_id) for item in reversed(admitted)
                )
                ordered = tuple(sorted(requested))

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
                        max_units=100_000,
                    )

                self.assertEqual(resolve.call_count, 1)
                self.assertEqual(artifact.call_count, 1)
                self.assertEqual(result.included_refs, (ordered[0],))
                self.assertEqual(result.excluded_refs, ordered[1:])
                self.assertIs(result.completeness, ContextCompleteness.TRUNCATED)
                self.assertLessEqual(result.used_units, result.budget_units)
            finally:
                programs.close()


if __name__ == "__main__":
    unittest.main()
