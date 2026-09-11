from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from ai_capital.kernel.context import ContextCompiler, ContextRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ContextPriority
from ai_capital.kernel.errors import IntegrityViolation
from ai_capital.kernel.events import event_digest_fields
from ai_capital.kernel.evidence_store import EvidenceRepository
from ai_capital.kernel.models import Event, Program
from ai_capital.kernel.schema_codec import record_from_json, record_to_json
from ai_capital.kernel.serialization import canonical_digest, to_canonical_data


class K8ReviewRound38Tests(unittest.TestCase):
    @staticmethod
    def _event(programs: ProgramRepository, event_id: str) -> Event:
        row = programs._db.execute(
            "SELECT event_json FROM events WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        assert row is not None
        event = record_from_json(Event, row["event_json"])
        assert isinstance(event, Event)
        return event

    @staticmethod
    def _digest_for(event: Event, *, payload: object, program_id: str | None) -> str:
        return event_digest_fields(
            event_id=event.event_id,
            sequence=event.sequence,
            event_type=event.event_type,
            occurred_at=event.occurred_at,
            recorded_at=event.recorded_at,
            payload=payload,
            actor_id=event.actor_id,
            program_id=program_id,
            causation_id=event.causation_id,
            correlation_id=event.correlation_id,
        )

    def test_selector_then_body_host_control_mutation_stays_invalid_across_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "host.db"
            programs = ProgramRepository(database)
            program = programs.create(
                Program("p-1", 0, "selector-first Host-control invalidation")
            )
            contexts = ContextRepository(programs)
            source_ref = contexts.persist_source(
                program.program_id,
                priority=ContextPriority.HOST_CONTROL,
                payload={"rule": "mandatory"},
            )
            event_id = source_ref.removeprefix("event:")
            event = self._event(programs, event_id)

            with programs._transaction():
                programs._db.execute(
                    "UPDATE events SET context_source_priority = ? WHERE event_id = ?",
                    (ContextPriority.ADVISORY_MEMORY.value, event_id),
                )
            marker = programs._db.execute(
                "SELECT program_id, priority FROM context_persisted_source_invalidations "
                "WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            self.assertIsNotNone(marker)
            self.assertEqual(marker["program_id"], program.program_id)
            self.assertEqual(marker["priority"], ContextPriority.HOST_CONTROL.value)

            payload = to_canonical_data(event.payload)
            source = payload["source"]
            source["priority"] = ContextPriority.ADVISORY_MEMORY.value
            source["currentness"] = "advisory"
            source["authority"] = "advisory"
            source["payload"] = {"rule": "forged"}
            source["source_digest"] = canonical_digest(source["payload"])
            digest = self._digest_for(event, payload=payload, program_id=event.program_id)
            forged = replace(event, payload=payload, digest=digest)
            with programs._transaction():
                programs._db.execute(
                    "UPDATE events SET event_json = ?, event_digest = ? WHERE event_id = ?",
                    (record_to_json(forged), digest, event_id),
                )
            programs.close()

            reopened = ProgramRepository(database)
            try:
                contexts = ContextRepository(reopened)
                with self.assertRaisesRegex(IntegrityViolation, "invalidation evidence"):
                    ContextCompiler(contexts).compile(
                        program.program_id,
                        budget_units=100000,
                    )
            finally:
                reopened.close()

    def test_legacy_evidence_rebuild_rejects_program_bound_admission_event(self):
        for legacy_version in (2, 3):
            with self.subTest(version=legacy_version), tempfile.TemporaryDirectory() as directory:
                database = Path(directory) / "host.db"
                programs = ProgramRepository(database)
                program = programs.create(
                    Program("p-1", 0, "legacy Evidence admission envelope")
                )
                evidence = EvidenceRepository(programs)
                item = evidence.admit(
                    content=b"round-38-evidence",
                    source_class="test",
                    observed_at="2026-01-01T00:00:00Z",
                    provenance=("round-38",),
                    trust_class="test",
                    currentness="current",
                )
                admitted_event_id = str(
                    evidence._row(item.evidence_id)["admitted_event_id"]
                )
                event = self._event(programs, admitted_event_id)
                payload = to_canonical_data(event.payload)
                digest = self._digest_for(
                    event,
                    payload=payload,
                    program_id=program.program_id,
                )
                forged = replace(
                    event,
                    program_id=program.program_id,
                    payload=payload,
                    digest=digest,
                )
                with programs._transaction():
                    programs._db.execute(
                        "DROP TRIGGER IF EXISTS evidence_admission_event_integrity_invalidate"
                    )
                    programs._db.execute(
                        "UPDATE component_schema SET version = ? "
                        "WHERE component = 'evidence_store'",
                        (legacy_version,),
                    )
                    programs._db.execute(
                        "UPDATE events SET program_id = ?, event_json = ?, event_digest = ? "
                        "WHERE event_id = ?",
                        (
                            program.program_id,
                            record_to_json(forged),
                            digest,
                            admitted_event_id,
                        ),
                    )
                programs.close()

                reopened = ProgramRepository(database)
                try:
                    with self.assertRaisesRegex(
                        IntegrityViolation,
                        "must remain Host-scoped during migration",
                    ):
                        EvidenceRepository(reopened)
                finally:
                    reopened.close()

    def test_compiled_event_type_invalidation_is_detected_before_projection_rebuild(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "host.db"
            programs = ProgramRepository(database)
            program = programs.create(
                Program("p-1", 0, "compiled Event type invalidation")
            )
            contexts = ContextRepository(programs)
            compiled = ContextCompiler(contexts).compile(
                program.program_id,
                budget_units=100000,
            )
            event_id = str(
                programs._db.execute(
                    "SELECT compiled_event_id FROM context_receipts "
                    "WHERE context_receipt_id = ?",
                    (compiled.receipt.context_receipt_id,),
                ).fetchone()[0]
            )
            indexed = programs._db.execute(
                "SELECT event_id FROM context_receipt_event_index WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            self.assertIsNotNone(indexed)
            with programs._transaction():
                programs._db.execute(
                    "UPDATE events SET event_type = 'context.invalidated' WHERE event_id = ?",
                    (event_id,),
                )
            invalidated = programs._db.execute(
                "SELECT context_recall_invalidated FROM events WHERE event_id = ?",
                (event_id,),
            ).fetchone()[0]
            self.assertEqual(int(invalidated), 1)
            programs.close()

            reopened = ProgramRepository(database)
            try:
                with self.assertRaisesRegex(
                    IntegrityViolation,
                    "compiled Context Event was invalidated",
                ):
                    ContextRepository(reopened)
            finally:
                reopened.close()


if __name__ == "__main__":
    unittest.main()
