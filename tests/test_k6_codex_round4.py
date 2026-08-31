from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.evidence_store import EvidenceRepository
from ai_capital.kernel.errors import IntegrityViolation
from ai_capital.kernel.models import Event
from ai_capital.kernel.schema_codec import record_from_json, record_to_json


OBSERVED = "2026-08-31T00:00:00Z"


class RecordingEvidenceRepository(EvidenceRepository):
    def __init__(self, *args, **kwargs):
        self.fsynced_directories: list[Path] = []
        super().__init__(*args, **kwargs)

    def _fsync_directory(self, path: Path) -> None:
        self.fsynced_directories.append(Path(path))


class K6CodexRound4Tests(unittest.TestCase):
    @staticmethod
    def _admit(
        evidence: EvidenceRepository,
        content: bytes,
        *,
        evidence_id: str | None = None,
    ):
        return evidence.admit(
            content=content,
            source_class="source_observation",
            observed_at=OBSERVED,
            provenance=("source:fixture", "admission:host"),
            trust_class="observed",
            currentness="current",
            evidence_id=evidence_id,
        )

    def test_explicit_artifact_root_fsyncs_every_created_ancestor(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            programs = ProgramRepository(root / "kernel.db")
            artifact_root = root / "a" / "b" / "c" / "evidence"
            try:
                evidence = RecordingEvidenceRepository(
                    programs,
                    artifact_root=artifact_root,
                )
                self.assertTrue(
                    {
                        root,
                        root / "a",
                        root / "a" / "b",
                        root / "a" / "b" / "c",
                    }.issubset(set(evidence.fsynced_directories))
                )
            finally:
                programs.close()

    def test_evidence_identity_lookup_does_not_decode_unrelated_events(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "kernel.db")
            evidence = EvidenceRepository(programs)
            try:
                first = self._admit(evidence, b"first", evidence_id="e-first")
                self._admit(evidence, b"second", evidence_id="e-second")

                row = programs._db.execute(
                    """
                    SELECT event_json, sequence FROM events
                    WHERE event_type = 'evidence.admitted'
                    ORDER BY sequence DESC LIMIT 1
                    """
                ).fetchone()
                self.assertIsNotNone(row)
                event = record_from_json(Event, row["event_json"])
                forged = event.__class__(
                    event_id=event.event_id,
                    sequence=event.sequence,
                    event_type=event.event_type,
                    occurred_at=event.occurred_at,
                    recorded_at=event.recorded_at,
                    payload=event.payload,
                    digest="0" * 64,
                    actor_id=event.actor_id,
                    program_id=event.program_id,
                    causation_id=event.causation_id,
                    correlation_id=event.correlation_id,
                )
                programs._db.execute(
                    "UPDATE events SET event_json = ? WHERE sequence = ?",
                    (record_to_json(forged), int(row["sequence"])),
                )

                third = self._admit(evidence, b"third", evidence_id="e-third")
                self.assertEqual(third.evidence_id, "e-third")
                self.assertEqual(evidence.get(first.evidence_id), first)
                with self.assertRaises(IntegrityViolation):
                    evidence.get("e-second")
            finally:
                programs.close()

    def test_evidence_event_index_migrates_from_schema_v1_and_is_indexed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            programs = ProgramRepository(root / "kernel.db")
            evidence = EvidenceRepository(programs)
            try:
                admitted = self._admit(
                    evidence,
                    b"migrate",
                    evidence_id="e-migrate",
                )
                programs._db.execute("DROP TABLE evidence_event_index")
                programs._db.execute(
                    "UPDATE component_schema SET version = 1 WHERE component = 'evidence_store'"
                )

                migrated = EvidenceRepository(programs)
                self.assertEqual(migrated.get(admitted.evidence_id), admitted)
                row = programs._db.execute(
                    """
                    SELECT sequence, evidence_id, event_id, event_type
                    FROM evidence_event_index WHERE evidence_id = ?
                    """,
                    (admitted.evidence_id,),
                ).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row["evidence_id"], admitted.evidence_id)
                self.assertEqual(row["event_type"], "evidence.admitted")

                plan = programs._db.execute(
                    """
                    EXPLAIN QUERY PLAN
                    SELECT sequence FROM evidence_event_index
                    WHERE evidence_id = ? ORDER BY sequence
                    """,
                    (admitted.evidence_id,),
                ).fetchall()
                detail = " ".join(str(item["detail"]) for item in plan)
                self.assertIn("evidence_event_index_identity_sequence", detail)
            finally:
                programs.close()


if __name__ == "__main__":
    unittest.main()
