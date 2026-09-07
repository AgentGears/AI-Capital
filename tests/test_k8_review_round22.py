from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from ai_capital.kernel.context import ContextCompiler, ContextRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.evidence_store import EvidenceRepository
from ai_capital.kernel.models import Program


class K8ReviewRound22Tests(unittest.TestCase):
    def test_current_evidence_storage_is_not_charged_to_context_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "current Evidence budget separation"))
                evidence = EvidenceRepository(programs)
                item = evidence.admit(content=b"small", source_class="test", observed_at="2026-01-01T00:00:00Z", provenance=("test",), trust_class="test", currentness="current")
                contexts = ContextRepository(programs, evidence)
                compiler = ContextCompiler(contexts, evidence=evidence)
                first = compiler.compile(program.program_id, budget_units=100000, evidence_refs=(item.evidence_id,))
                exact = compiler.compile(program.program_id, budget_units=first.used_units, evidence_refs=(item.evidence_id,))
                ref = f"evidence:{item.evidence_id}"
                self.assertIn(ref, exact.receipt.included_refs)
                self.assertNotIn(ref, exact.receipt.excluded_refs)
                self.assertEqual(exact.used_units, first.used_units)
            finally:
                programs.close()

    def test_compiled_event_storage_is_not_charged_to_recall_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "historical Context budget separation"))
                contexts = ContextRepository(programs)
                compiled = ContextCompiler(contexts).compile(program.program_id, budget_units=100000)
                first = contexts.recall(program.program_id, (compiled.receipt.context_receipt_id,), max_items=1, max_units=100000)
                exact = contexts.recall(program.program_id, (compiled.receipt.context_receipt_id,), max_items=1, max_units=first.used_units)
                self.assertEqual(exact.included_refs, (compiled.receipt.context_receipt_id,))
                self.assertEqual(exact.excluded_refs, ())
                self.assertEqual(exact.used_units, first.used_units)
            finally:
                programs.close()

    def test_v3_context_schema_advances_to_current_version(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "host.db"
            programs = ProgramRepository(path)
            try:
                ContextRepository(programs)
                programs._db.execute("UPDATE component_schema SET version = 3 WHERE component = 'bounded_context'")
            finally:
                programs.close()
            programs = ProgramRepository(path)
            try:
                ContextRepository(programs)
                row = programs._db.execute("SELECT version FROM component_schema WHERE component = 'bounded_context'").fetchone()
                self.assertEqual(int(row["version"]), 5)
            finally:
                programs.close()


if __name__ == "__main__":
    unittest.main()
