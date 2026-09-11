from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_capital.kernel.context import ContextCompiler, ContextRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ContextPriority
from ai_capital.kernel.errors import InvalidRequest
from ai_capital.kernel.evidence_store import EvidenceRepository
from ai_capital.kernel.models import Program


class K8ReviewRound11Tests(unittest.TestCase):
    def test_oversized_recalled_evidence_is_excluded_before_artifact_materialization(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "bounded recall preflight"))
                evidence = EvidenceRepository(programs)
                item = evidence.admit(content=b"x" * 131072, source_class="test", observed_at="2026-01-01T00:00:00Z", provenance=("test",), trust_class="test", currentness="current")
                contexts = ContextRepository(programs, evidence)
                with patch.object(evidence, "artifact", side_effect=AssertionError("oversized Evidence artifact was materialized")) as artifact:
                    result = contexts.recall(program.program_id, (f"evidence:{item.evidence_id}",), max_items=1, max_units=14)
                artifact.assert_not_called()
                self.assertEqual(result.included_refs, ())
            finally:
                programs.close()

    def test_oversized_recalled_context_is_excluded_before_context_decode(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "bounded Context recall"))
                contexts = ContextRepository(programs)
                compiler = ContextCompiler(contexts)
                persisted = contexts.persist_source(program.program_id, priority=ContextPriority.ADVISORY_MEMORY, payload={"blob": "x" * 131072})
                compiled = compiler.compile(program.program_id, budget_units=300000, source_refs=(persisted,))
                with patch.object(contexts, "get", side_effect=AssertionError("oversized historical Context was decoded")) as get_context:
                    result = contexts.recall(program.program_id, (compiled.receipt.context_receipt_id,), max_items=1, max_units=14)
                get_context.assert_not_called()
                self.assertEqual(result.included_refs, ())
            finally:
                programs.close()

    def test_duplicate_current_and_recalled_evidence_rejected_before_recall_truncation(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "duplicate source identity"))
                evidence = EvidenceRepository(programs)
                item = evidence.admit(content=b"x" * 4096, source_class="test", observed_at="2026-01-01T00:00:00Z", provenance=("test",), trust_class="test", currentness="current")
                contexts = ContextRepository(programs, evidence)
                compiler = ContextCompiler(contexts, evidence=evidence)
                ref = f"evidence:{item.evidence_id}"
                with patch.object(contexts, "recall", side_effect=AssertionError("duplicate request reached truncating recall")) as recall:
                    with self.assertRaises(InvalidRequest):
                        compiler.compile(program.program_id, budget_units=100000, evidence_refs=(item.evidence_id,), recalled_refs=(ref,), recall_max_items=1, recall_max_units=14)
                recall.assert_not_called()
            finally:
                programs.close()
