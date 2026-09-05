from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_capital.kernel.claim_store import ClaimRepository
from ai_capital.kernel.context import ContextRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ContextPriority
from ai_capital.kernel.errors import InvalidRequest
from ai_capital.kernel.evidence_store import EvidenceRepository
from ai_capital.kernel.models import Program


class K8ReviewRound10Tests(unittest.TestCase):
    def test_rebuild_does_not_bind_evidence_identity_collision_to_program(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("shared-id", 0, "explicit Event ownership"))
                evidence = EvidenceRepository(programs)
                evidence.admit(
                    content=b"evidence payload",
                    source_class="test",
                    observed_at="2026-01-01T00:00:00Z",
                    provenance=("test",),
                    trust_class="test",
                    currentness="current",
                    evidence_id=program.program_id,
                )
                event_id = str(
                    programs._db.execute(
                        "SELECT admitted_event_id FROM evidence_records WHERE evidence_id = ?",
                        (program.program_id,),
                    ).fetchone()["admitted_event_id"]
                )

                contexts = ContextRepository(programs, evidence)
                indexed = programs._db.execute(
                    "SELECT program_id FROM context_recall_event_index WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
                self.assertIsNotNone(indexed)
                self.assertIsNone(indexed["program_id"])

                with patch.object(
                    contexts,
                    "_event_by_id",
                    side_effect=AssertionError("unowned Evidence Event was materialized"),
                ) as event_by_id:
                    with self.assertRaises(InvalidRequest):
                        contexts.recall(
                            program.program_id,
                            (f"event:{event_id}",),
                            max_items=1,
                            max_units=100_000,
                        )
                event_by_id.assert_not_called()
            finally:
                programs.close()

    def test_claim_identity_collision_is_rejected_even_if_recall_index_is_polluted(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("shared-id", 0, "claim ownership isolation"))
                evidence = EvidenceRepository(programs)
                contexts = ContextRepository(programs, evidence)
                claims = ClaimRepository(programs, evidence)
                claims.create("unrelated claim", claim_id=program.program_id)
                event_id = str(
                    programs._db.execute(
                        """
                        SELECT events.event_id
                        FROM claim_event_index
                        JOIN events ON events.sequence = claim_event_index.sequence
                        WHERE claim_event_index.claim_id = ?
                        ORDER BY claim_event_index.sequence LIMIT 1
                        """,
                        (program.program_id,),
                    ).fetchone()["event_id"]
                )
                indexed = programs._db.execute(
                    "SELECT program_id FROM context_recall_event_index WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
                self.assertIsNotNone(indexed)
                self.assertIsNone(indexed["program_id"])

                programs._db.execute(
                    "UPDATE context_recall_event_index SET program_id = ? WHERE event_id = ?",
                    (program.program_id, event_id),
                )
                with patch.object(
                    contexts,
                    "_event_by_id",
                    side_effect=AssertionError("unowned Claim Event was materialized"),
                ) as event_by_id:
                    with self.assertRaises(InvalidRequest):
                        contexts.recall(
                            program.program_id,
                            (f"event:{event_id}",),
                            max_items=1,
                            max_units=100_000,
                        )
                event_by_id.assert_not_called()
            finally:
                programs.close()

    def test_program_scoped_context_correlation_remains_recallable(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "valid correlation ownership"))
                contexts = ContextRepository(programs)
                ref = contexts.persist_source(
                    program.program_id,
                    priority=ContextPriority.ADVISORY_MEMORY,
                    payload={"memory": "durable"},
                )
                event_id = ref.removeprefix("event:")
                indexed = programs._db.execute(
                    "SELECT program_id FROM context_recall_event_index WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
                self.assertEqual(indexed["program_id"], program.program_id)

                result = contexts.recall(
                    program.program_id,
                    (ref,),
                    max_items=1,
                    max_units=100_000,
                )
                self.assertEqual(result.included_refs, (ref,))
            finally:
                programs.close()


if __name__ == "__main__":
    unittest.main()
