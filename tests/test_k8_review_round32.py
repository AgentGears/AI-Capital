from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_capital.kernel.context import ContextCompiler, ContextRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.errors import IntegrityViolation
from ai_capital.kernel.evidence_store import EvidenceRepository
from ai_capital.kernel.models import Program


class K8ReviewRound32Tests(unittest.TestCase):
    def _admit_current(self, evidence: EvidenceRepository):
        return evidence.admit(
            content=b"round-32-evidence",
            source_class="test",
            observed_at="2026-01-01T00:00:00Z",
            provenance=("round-32",),
            trust_class="test",
            currentness="current",
        )

    def test_current_evidence_event_mutation_fails_before_budget_exclusion(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "authenticate Evidence before exclusion"))
                evidence = EvidenceRepository(programs)
                item = self._admit_current(evidence)
                contexts = ContextRepository(programs, evidence)
                compiler = ContextCompiler(contexts, evidence=evidence)
                baseline = compiler.compile(program.program_id, budget_units=100000)
                admitted_event_id = str(evidence._row(item.evidence_id)["admitted_event_id"])
                programs._db.execute(
                    "UPDATE events SET event_json = event_json || ? WHERE event_id = ?",
                    (" ", admitted_event_id),
                )
                with patch.object(
                    evidence,
                    "_row",
                    side_effect=AssertionError("corrupt Evidence reached full record decode"),
                ) as full_row:
                    with self.assertRaisesRegex(
                        IntegrityViolation, "Evidence preflight Event binding mismatch"
                    ):
                        compiler.compile(
                            program.program_id,
                            budget_units=baseline.used_units,
                            evidence_refs=(item.evidence_id,),
                        )
                full_row.assert_not_called()
            finally:
                programs.close()

    def test_event_invalidation_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "host.db"
            programs = ProgramRepository(database)
            program = programs.create(Program("p-1", 0, "durable Evidence invalidation"))
            evidence = EvidenceRepository(programs)
            item = self._admit_current(evidence)
            admitted_event_id = str(evidence._row(item.evidence_id)["admitted_event_id"])
            programs._db.execute(
                "UPDATE events SET event_json = event_json || ? WHERE event_id = ?",
                (" ", admitted_event_id),
            )
            programs.close()

            reopened = ProgramRepository(database)
            try:
                evidence_reopened = EvidenceRepository(reopened)
                contexts = ContextRepository(reopened, evidence_reopened)
                compiler = ContextCompiler(contexts, evidence=evidence_reopened)
                with self.assertRaisesRegex(
                    IntegrityViolation, "Evidence preflight Event binding mismatch"
                ):
                    compiler.compile(
                        program.program_id,
                        budget_units=100000,
                        evidence_refs=(item.evidence_id,),
                    )
            finally:
                reopened.close()

    def test_v3_migration_reauthenticates_existing_evidence_events(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "host.db"
            programs = ProgramRepository(database)
            evidence = EvidenceRepository(programs)
            item = self._admit_current(evidence)
            admitted_event_id = str(evidence._row(item.evidence_id)["admitted_event_id"])
            programs._db.execute(
                "DROP TRIGGER IF EXISTS evidence_admission_event_integrity_invalidate"
            )
            programs._db.execute(
                "UPDATE component_schema SET version = 3 WHERE component = 'evidence_store'"
            )
            programs._db.execute(
                "UPDATE events SET event_digest = ? WHERE event_id = ?",
                ("0" * 64, admitted_event_id),
            )
            programs.close()

            reopened = ProgramRepository(database)
            try:
                with self.assertRaisesRegex(IntegrityViolation, "Evidence Event integrity mismatch"):
                    EvidenceRepository(reopened)
            finally:
                reopened.close()


if __name__ == "__main__":
    unittest.main()
