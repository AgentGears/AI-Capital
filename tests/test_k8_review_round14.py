from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_capital.kernel.context import ContextCompiler, ContextRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ContextPriority
from ai_capital.kernel.errors import (
    ContextBudgetExceeded,
    ContextIncomplete,
    IntegrityViolation,
)
from ai_capital.kernel.evidence_store import EvidenceRepository
from ai_capital.kernel.models import Program
from ai_capital.kernel.serialization import canonical_json, to_canonical_data


class K8ReviewRound14Tests(unittest.TestCase):
    def test_compile_does_not_replay_program_history_on_hot_path(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "bounded Program hot path"))
                contexts = ContextRepository(programs)
                compiler = ContextCompiler(contexts)
                with patch.object(
                    programs,
                    "verify_integrity",
                    side_effect=AssertionError("Context compile replayed Program history"),
                ) as verify_integrity, patch.object(
                    programs,
                    "list_events",
                    side_effect=AssertionError("Context compile scanned Program history"),
                ) as list_events:
                    compiled = compiler.compile(program.program_id, budget_units=100_000)
                verify_integrity.assert_not_called()
                list_events.assert_not_called()
                self.assertEqual(compiled.receipt.program_id, program.program_id)
            finally:
                programs.close()

    def test_oversized_current_program_is_rejected_before_projection_decode(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-large", 0, "x" * 131072))
                contexts = ContextRepository(programs)
                compiler = ContextCompiler(contexts)
                with patch.object(
                    programs,
                    "get",
                    side_effect=AssertionError("oversized Program projection was decoded"),
                ) as get_program:
                    with self.assertRaises(ContextBudgetExceeded):
                        compiler.compile(program.program_id, budget_units=128)
                get_program.assert_not_called()
            finally:
                programs.close()

    def test_stale_current_evidence_is_rejected_before_budget_truncation(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "currentness before truncation"))
                evidence = EvidenceRepository(programs)
                stale = evidence.admit(
                    content=b"small",
                    source_class="test",
                    observed_at="2026-01-01T00:00:00Z",
                    provenance=("p" * 131072,),
                    trust_class="test",
                    currentness="stale",
                )
                contexts = ContextRepository(programs, evidence)
                compiler = ContextCompiler(contexts, evidence=evidence)
                baseline = compiler.compile(program.program_id, budget_units=100_000)
                with patch.object(
                    evidence,
                    "_row",
                    side_effect=AssertionError("stale Evidence record was decoded"),
                ) as full_row:
                    with self.assertRaises(ContextIncomplete):
                        compiler.compile(
                            program.program_id,
                            budget_units=baseline.used_units,
                            evidence_refs=(stale.evidence_id,),
                        )
                full_row.assert_not_called()
            finally:
                programs.close()

    def test_currentness_projection_corruption_is_rejected_before_decode(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "currentness projection integrity"))
                evidence = EvidenceRepository(programs)
                item = evidence.admit(
                    content=b"small",
                    source_class="test",
                    observed_at="2026-01-01T00:00:00Z",
                    provenance=("test",),
                    trust_class="test",
                    currentness="current",
                )
                contexts = ContextRepository(programs, evidence)
                compiler = ContextCompiler(contexts, evidence=evidence)
                programs._db.execute(
                    "UPDATE evidence_records SET currentness = 'stale' WHERE evidence_id = ?",
                    (item.evidence_id,),
                )
                with patch.object(
                    evidence,
                    "_row",
                    side_effect=AssertionError("corrupt Evidence projection was decoded"),
                ) as full_row:
                    with self.assertRaises(IntegrityViolation):
                        compiler.compile(
                            program.program_id,
                            budget_units=100_000,
                            evidence_refs=(item.evidence_id,),
                        )
                full_row.assert_not_called()
            finally:
                programs.close()

    def test_scoped_event_program_index_corruption_is_rejected_before_truncation(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                first = programs.create(Program("p-1", 0, "first Program"))
                second = programs.create(Program("p-2", 0, "second Program"))
                contexts = ContextRepository(programs)
                ref = contexts.persist_source(
                    first.program_id,
                    priority=ContextPriority.ADVISORY_MEMORY,
                    payload={"memory": "first"},
                )
                event_id = ref.removeprefix("event:")
                indexed = programs._db.execute(
                    "SELECT program_id, correlation_id FROM context_recall_event_index "
                    "WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
                self.assertEqual(indexed["program_id"], first.program_id)
                self.assertEqual(indexed["correlation_id"], first.program_id)
                programs._db.execute(
                    "UPDATE context_recall_event_index SET program_id = ? WHERE event_id = ?",
                    (second.program_id, event_id),
                )
                with patch.object(
                    contexts,
                    "_event_by_id",
                    side_effect=AssertionError("corrupt scoped Event was materialized"),
                ) as event_by_id:
                    with self.assertRaises(IntegrityViolation):
                        contexts.recall(
                            second.program_id,
                            (ref,),
                            max_items=1,
                            max_units=14,
                        )
                event_by_id.assert_not_called()
            finally:
                programs.close()

    def test_historical_evidence_recall_includes_admission_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "Evidence admission recall"))
                evidence = EvidenceRepository(programs)
                item = evidence.admit(
                    content=b"exact evidence",
                    source_class="test",
                    observed_at="2026-01-01T00:00:00Z",
                    provenance=("test",),
                    trust_class="test",
                    currentness="current",
                )
                admission = evidence.admission(item.evidence_id)
                contexts = ContextRepository(programs, evidence)
                result = contexts.recall(
                    program.program_id,
                    (f"evidence:{item.evidence_id}",),
                    max_items=1,
                    max_units=100_000,
                )
                self.assertEqual(len(result.items), 1)
                recalled_admission = result.items[0].payload["admission"]
                self.assertEqual(
                    canonical_json(recalled_admission),
                    canonical_json(to_canonical_data(admission)),
                )
            finally:
                programs.close()


if __name__ == "__main__":
    unittest.main()
