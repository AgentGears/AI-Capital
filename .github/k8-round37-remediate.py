from pathlib import Path

context_path = Path("src/ai_capital/kernel/context.py")
context = context_path.read_text()
old_trigger = """                    WHERE OLD.event_type = 'context.source_persisted'
                      AND OLD.context_source_priority = 'host_control'
                      AND OLD.context_source_program_id IS NOT NULL
                      AND OLD.context_source_program_revision IS NOT NULL
                      AND OLD.context_source_metadata_digest IS NOT NULL
                      AND OLD.event_type IS NOT NEW.event_type;
"""
new_trigger = """                    WHERE OLD.event_type = 'context.source_persisted'
                      AND OLD.context_source_priority = 'host_control'
                      AND OLD.context_source_program_id IS NOT NULL
                      AND OLD.context_source_program_revision IS NOT NULL
                      AND OLD.context_source_metadata_digest IS NOT NULL
                      AND (
                          OLD.event_type IS NOT NEW.event_type
                          OR OLD.event_json IS NOT NEW.event_json
                          OR OLD.event_digest IS NOT NEW.event_digest
                      );
"""
if old_trigger not in context:
    raise SystemExit("Round 37 Host-control trigger anchor not found")
context = context.replace(old_trigger, new_trigger, 1)

old_receipts = '''                SELECT sequence, event_id, program_id, event_type, event_json, event_digest
                FROM events
                WHERE event_type = 'context.compiled' AND sequence > ?
                ORDER BY sequence
                LIMIT 1
                """,
                (last_sequence,),
            ).fetchone()
            if row is None:
                break
            event = self._decode_event_row(row)
'''
new_receipts = '''                SELECT sequence, event_id, program_id, event_type, event_json, event_digest,
                       context_recall_invalidated
                FROM events
                WHERE event_type = 'context.compiled' AND sequence > ?
                ORDER BY sequence
                LIMIT 1
                """,
                (last_sequence,),
            ).fetchone()
            if row is None:
                break
            try:
                invalidated = int(row["context_recall_invalidated"])
            except (TypeError, ValueError) as exc:
                raise IntegrityViolation(
                    "compiled Context Event invalidation marker is malformed"
                ) from exc
            if invalidated != 0:
                raise IntegrityViolation("compiled Context Event was invalidated")
            event = self._decode_event_row(row)
'''
if old_receipts not in context:
    raise SystemExit("Round 37 receipt iterator anchor not found")
context = context.replace(old_receipts, new_receipts, 1)
context_path.write_text(context)

evidence_path = Path("src/ai_capital/kernel/evidence_store.py")
evidence = evidence_path.read_text()
anchor = '''    def _rebuild_event_index(self) -> None:
        self._host_store._db.execute("DELETE FROM evidence_event_index")
'''
helper = '''    def _validate_rebuilt_event_record_binding(self, event: Event) -> None:
        row = self._host_store._db.execute(
            """
            SELECT evidence_id, artifact_digest, admitted_event_id,
                   evidence_json, evidence_record_digest,
                   admission_json, admission_digest
            FROM evidence_records WHERE admitted_event_id = ?
            """,
            (event.event_id,),
        ).fetchone()
        if row is None:
            raise IntegrityViolation(
                "Evidence admission Event lacks its durable Evidence record"
            )
        try:
            evidence = record_from_json(Evidence, row["evidence_json"])
            admission = record_from_json(
                EvidenceAdmissionReceipt,
                row["admission_json"],
            )
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation(
                "Evidence migration record binding cannot be decoded"
            ) from exc
        if not isinstance(evidence, Evidence) or not isinstance(
            admission, EvidenceAdmissionReceipt
        ):
            raise IntegrityViolation(
                "Evidence migration record binding decoded wrong type"
            )
        if (
            evidence.evidence_id != row["evidence_id"]
            or evidence.digest != row["artifact_digest"]
            or canonical_digest(evidence) != row["evidence_record_digest"]
            or canonical_digest(admission) != row["admission_digest"]
            or admission.evidence_id != evidence.evidence_id
            or admission.artifact_digest != evidence.digest
            or row["admitted_event_id"] != event.event_id
        ):
            raise IntegrityViolation(
                "Evidence migration record binding is inconsistent"
            )
        self._validate_evidence(evidence)
        expected_payload = to_canonical_data(
            {"evidence": evidence, "admission": admission}
        )
        if (
            event.correlation_id != evidence.evidence_id
            or to_canonical_data(event.payload) != expected_payload
        ):
            raise IntegrityViolation(
                "Evidence record diverges from admission Event during migration"
            )

    def _rebuild_event_index(self) -> None:
        self._host_store._db.execute("DELETE FROM evidence_event_index")
'''
if anchor not in evidence:
    raise SystemExit("Round 37 Evidence rebuild anchor not found")
evidence = evidence.replace(anchor, helper, 1)
old_index = '''            if not event.correlation_id:
                raise IntegrityViolation("Evidence admission Event lacks Evidence identity")
            self._host_store._db.execute(
'''
new_index = '''            if not event.correlation_id:
                raise IntegrityViolation("Evidence admission Event lacks Evidence identity")
            self._validate_rebuilt_event_record_binding(event)
            self._host_store._db.execute(
'''
if old_index not in evidence:
    raise SystemExit("Round 37 Evidence index insertion anchor not found")
evidence = evidence.replace(old_index, new_index, 1)
evidence_path.write_text(evidence)

Path("tests/test_k8_review_round37.py").write_text(r'''from __future__ import annotations

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
                    IntegrityViolation, "Host-control invalidation evidence"
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
''')
