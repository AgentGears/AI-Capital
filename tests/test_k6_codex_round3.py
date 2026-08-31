from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from ai_capital.kernel.claim_store import ClaimRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.errors import IntegrityViolation
from ai_capital.kernel.evidence_store import EvidenceRepository
from ai_capital.kernel.models import Claim, Event
from ai_capital.kernel.schema_codec import record_from_json, record_to_json
from ai_capital.kernel.serialization import canonical_digest


OBSERVED = "2026-08-31T00:00:00Z"


class K6CodexRound3Tests(unittest.TestCase):
    def _stores(self, directory: str):
        programs = ProgramRepository(Path(directory) / "kernel.db")
        evidence = EvidenceRepository(programs)
        claims = ClaimRepository(programs, evidence)
        return programs, evidence, claims

    @staticmethod
    def _admit(evidence: EvidenceRepository, content: bytes):
        return evidence.admit(
            content=content,
            source_class="source_observation",
            observed_at=OBSERVED,
            provenance=("source:fixture", "admission:host"),
            trust_class="observed",
            currentness="current",
        )

    def test_get_rejects_modified_intermediate_claim_history_row(self):
        with tempfile.TemporaryDirectory() as directory:
            programs, evidence, claims = self._stores(directory)
            try:
                support = self._admit(evidence, b"support")
                contradiction = self._admit(evidence, b"contradiction")
                claim = claims.create("statement")
                claims.support(claim.claim_id, (support.evidence_id,))
                claims.contradict(claim.claim_id, (contradiction.evidence_id,))

                row = programs._db.execute(
                    """
                    SELECT sequence, claim_json FROM claim_history
                    WHERE claim_id = ? AND event_type = 'claim.supported'
                    """,
                    (claim.claim_id,),
                ).fetchone()
                self.assertIsNotNone(row)
                historical = record_from_json(Claim, row["claim_json"])
                forged = replace(historical, statement="forged intermediate state")
                programs._db.execute(
                    """
                    UPDATE claim_history
                    SET claim_json = ?, claim_digest = ?
                    WHERE sequence = ?
                    """,
                    (
                        record_to_json(forged),
                        canonical_digest(forged),
                        int(row["sequence"]),
                    ),
                )

                with self.assertRaises(IntegrityViolation):
                    claims.get(claim.claim_id)
            finally:
                programs.close()

    def test_claim_read_does_not_decode_unrelated_claim_events(self):
        with tempfile.TemporaryDirectory() as directory:
            programs, _, claims = self._stores(directory)
            try:
                first = claims.create("first", claim_id="claim-a")
                claims.create("second", claim_id="claim-b")

                row = programs._db.execute(
                    """
                    SELECT sequence, event_json FROM events
                    WHERE event_type = 'claim.created'
                    ORDER BY sequence DESC LIMIT 1
                    """
                ).fetchone()
                self.assertIsNotNone(row)
                event = record_from_json(Event, row["event_json"])
                forged = replace(event, digest="0" * 64)
                programs._db.execute(
                    """
                    UPDATE events SET event_json = ? WHERE sequence = ?
                    """,
                    (record_to_json(forged), int(row["sequence"])),
                )

                # A scoped lookup must not load or validate Claim B's Event.
                self.assertEqual(claims.get(first.claim_id), first)
                with self.assertRaises(IntegrityViolation):
                    claims.get("claim-b")
            finally:
                programs.close()

    def test_claim_event_index_is_used_and_migrates_from_schema_v1(self):
        with tempfile.TemporaryDirectory() as directory:
            programs, _, claims = self._stores(directory)
            try:
                claim = claims.create("migrated", claim_id="claim-migrate")
                programs._db.execute("DROP TABLE claim_event_index")
                programs._db.execute(
                    "UPDATE component_schema SET version = 1 WHERE component = 'claim_store'"
                )

                migrated = ClaimRepository(programs, claims._evidence)
                self.assertEqual(migrated.get(claim.claim_id), claim)
                row = programs._db.execute(
                    """
                    SELECT sequence, claim_id, event_type FROM claim_event_index
                    WHERE claim_id = ?
                    """,
                    (claim.claim_id,),
                ).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row["claim_id"], claim.claim_id)
                self.assertEqual(row["event_type"], "claim.created")

                plan = programs._db.execute(
                    """
                    EXPLAIN QUERY PLAN
                    SELECT sequence FROM claim_event_index
                    WHERE claim_id = ? ORDER BY sequence
                    """,
                    (claim.claim_id,),
                ).fetchall()
                detail = " ".join(str(item["detail"]) for item in plan)
                self.assertIn("claim_event_index_claim_sequence", detail)
            finally:
                programs.close()


if __name__ == "__main__":
    unittest.main()
