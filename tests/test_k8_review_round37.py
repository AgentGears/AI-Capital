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


class K8ReviewRound37Tests(unittest.TestCase):
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
    def _rewrite_event(
        programs: ProgramRepository,
        event: Event,
        payload: object,
    ) -> Event:
        digest = event_digest_fields(
            event_id=event.event_id,
            sequence=event.sequence,
            event_type=event.event_type,
            occurred_at=event.occurred_at,
            recorded_at=event.recorded_at,
            payload=payload,
            actor_id=event.actor_id,
            program_id=event.program_id,
            causation_id=event.causation_id,
            correlation_id=event.correlation_id,
        )
        forged = replace(event, payload=payload, digest=digest)
        programs._db.execute(
            "UPDATE events SET event_json = ?, event_digest = ? WHERE event_id = ?",
            (record_to_json(forged), digest, event.event_id),
        )
        return forged

    def test_same_selector_host_control_mutation_persists_invalidation_across_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "host.db"
            programs = ProgramRepository(database)
            program = programs.create(
                Program("p-1", 0, "same-selector Host-control mutation")
            )
            contexts = ContextRepository(programs)
            source_ref = contexts.persist_source(
                program.program_id,
                priority=ContextPriority.HOST_CONTROL,
                payload={"rule": "mandatory"},
            )
            event_id = source_ref.removeprefix("event:")
            event = self._event(programs, event_id)
            payload = to_canonical_data(event.payload)
            source = payload["source"]
            source["payload"] = {"rule": "forged!!!"}
            source["source_digest"] = canonical_digest(source["payload"])
            self._rewrite_event(programs, event, payload)

            marker = programs._db.execute(
                "SELECT event_id FROM context_persisted_source_invalidations "
                "WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            self.assertIsNotNone(marker)
            programs.close()

            reopened = ProgramRepository(database)
            try:
                contexts = ContextRepository(reopened)
                marker = reopened._db.execute(
                    "SELECT event_id FROM context_persisted_source_invalidations "
                    "WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
                self.assertIsNotNone(marker)
                with self.assertRaisesRegex(
                    IntegrityViolation, "invalidation evidence"
                ):
                    ContextCompiler(contexts).compile(
                        program.program_id,
                        budget_units=100000,
                    )
            finally:
                reopened.close()

    def test_invalidated_compiled_event_fails_receipt_rebuild_on_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "host.db"
            programs = ProgramRepository(database)
            program = programs.create(
                Program("p-1", 0, "compiled Event restart authentication")
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
            event = self._event(programs, event_id)
            payload = to_canonical_data(event.payload)
            source = payload["context"]["sources"][0]
            program_payload = source["payload"]["program"]
            original_objective = str(program_payload["objective"])
            program_payload["objective"] = "x" * len(original_objective)
            source["source_digest"] = canonical_digest(source["payload"])
            self._rewrite_event(programs, event, payload)
            invalidated = programs._db.execute(
                "SELECT context_recall_invalidated FROM events WHERE event_id = ?",
                (event_id,),
            ).fetchone()[0]
            self.assertEqual(int(invalidated), 1)
            programs.close()

            reopened = ProgramRepository(database)
            try:
                with self.assertRaisesRegex(
                    IntegrityViolation, "compiled Context Event was invalidated"
                ):
                    ContextRepository(reopened)
            finally:
                reopened.close()

    def test_legacy_evidence_rebuild_rejects_payload_record_divergence(self):
        for legacy_version in (2, 3):
            with self.subTest(version=legacy_version), tempfile.TemporaryDirectory() as directory:
                database = Path(directory) / "host.db"
                programs = ProgramRepository(database)
                evidence = EvidenceRepository(programs)
                item = evidence.admit(
                    content=b"round-37-evidence",
                    source_class="test",
                    observed_at="2026-01-01T00:00:00Z",
                    provenance=("round-37",),
                    trust_class="test",
                    currentness="current",
                )
                admitted_event_id = str(
                    evidence._row(item.evidence_id)["admitted_event_id"]
                )
                event = self._event(programs, admitted_event_id)
                payload = to_canonical_data(event.payload)
                payload["evidence"]["trust_class"] = "forged"
                programs._db.execute(
                    "DROP TRIGGER IF EXISTS evidence_admission_event_integrity_invalidate"
                )
                programs._db.execute(
                    "UPDATE component_schema SET version = ? "
                    "WHERE component = 'evidence_store'",
                    (legacy_version,),
                )
                self._rewrite_event(programs, event, payload)
                programs.close()

                reopened = ProgramRepository(database)
                try:
                    with self.assertRaisesRegex(
                        IntegrityViolation,
                        "Evidence record diverges from admission Event during migration",
                    ):
                        EvidenceRepository(reopened)
                finally:
                    reopened.close()


if __name__ == "__main__":
    unittest.main()
