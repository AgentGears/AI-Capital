from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from ai_capital.kernel.actor_store import ActorRepository
from ai_capital.kernel.authority import AuthorityEngine, PolicySnapshot
from ai_capital.kernel.authority_store import AuthorityRepository
from ai_capital.kernel.builtin_capabilities import install_builtin_capabilities
from ai_capital.kernel.capability_broker import CapabilityBroker, CapabilityHandlerRegistry
from ai_capital.kernel.capability_store import CapabilityRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import (
    EffectClass,
    EffectStatus,
    ExecutionOutcome,
    ProgramStatus,
    ReconciliationStatus,
    RiskClass,
)
from ai_capital.kernel.errors import IntegrityViolation
from ai_capital.kernel.events import event_digest_fields
from ai_capital.kernel.frozen_json import FrozenMap
from ai_capital.kernel.models import (
    Actor,
    CapabilityRequest,
    CapabilityResolution,
    Event,
    Grant,
    Program,
    ResolvedEffect,
)
from ai_capital.kernel.operation_journal import (
    ExecutionObservation,
    ExecutionReceipt,
    OperationJournal,
    ReconciliationObservation,
)
from ai_capital.kernel.schema_codec import record_from_json, record_to_json
from ai_capital.kernel.serialization import to_canonical_data
from ai_capital.product import LocalProgramOperator


NOW = "2026-09-11T00:00:00Z"


def _redigest_event(event: Event, **changes) -> Event:
    draft = replace(event, **changes)
    digest = event_digest_fields(
        event_id=draft.event_id,
        sequence=draft.sequence,
        event_type=draft.event_type,
        occurred_at=draft.occurred_at,
        recorded_at=draft.recorded_at,
        payload=to_canonical_data(draft.payload),
        actor_id=draft.actor_id,
        program_id=draft.program_id,
        causation_id=draft.causation_id,
        correlation_id=draft.correlation_id,
    )
    return replace(draft, digest=digest)


def _write_event(programs: ProgramRepository, event: Event) -> None:
    programs._db.execute(
        """
        UPDATE events
        SET program_id = ?, event_type = ?, event_json = ?, event_digest = ?
        WHERE event_id = ?
        """,
        (
            event.program_id,
            event.event_type,
            record_to_json(event),
            event.digest,
            event.event_id,
        ),
    )


def _operation_resolution() -> CapabilityResolution:
    return CapabilityResolution(
        "req-op",
        "workspace.write",
        0,
        {"path": "notes.txt", "content": "updated"},
        ResolvedEffect(
            "workspace_path",
            "notes.txt",
            EffectClass.MODIFY,
            {"content": "updated"},
        ),
    )


def _create_ask(database: Path) -> str:
    with ProgramRepository(database) as programs:
        programs.create(Program("p-1", 0, "review approval"))
        programs.transition("p-1", ProgramStatus.ACTIVE, expected_revision=0)
        actors = ActorRepository(programs)
        actors.register(Actor("a-1", 0, "worker", "binding-a"))
        capabilities = CapabilityRepository(programs)
        handlers = CapabilityHandlerRegistry()
        install_builtin_capabilities(capabilities, handlers)
        broker = CapabilityBroker(capabilities, handlers)
        authority_store = AuthorityRepository(programs)
        authority_store.install_policy(
            PolicySnapshot(0, (RiskClass.MEDIUM,), (), NOW)
        )
        authority_store.issue_grant(
            Grant(
                "g-1",
                "actor:a-1",
                ("workspace.write",),
                ("*",),
                EffectClass.MODIFY,
                (),
                NOW,
                None,
                0,
            )
        )
        resolution = broker.resolve(
            CapabilityRequest(
                "req-ask",
                "workspace.write",
                {"path": "notes.txt", "content": "updated"},
                0,
            ),
            snapshot=broker.snapshot(("workspace.write",)),
        )
        engine = AuthorityEngine(
            programs,
            actors,
            capabilities,
            authority_store,
        )
        return engine.decide(
            program_id="p-1",
            actor_id="a-1",
            resolution=resolution,
        ).decision.decision_id


class H2ApprovalAuditReviewTests(unittest.TestCase):
    def test_operation_audit_does_not_bootstrap_unrelated_components(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "capital.db"
            with ProgramRepository(database) as programs:
                programs.create(Program("p-1", 0, "read only audit"))
                programs.transition("p-1", ProgramStatus.ACTIVE, expected_revision=0)
                journal = OperationJournal(programs)
                operation = journal.create_intent(
                    program_id="p-1",
                    actor_id="a-1",
                    resolution=_operation_resolution(),
                    authority_receipt_ref="authority-1",
                )
                journal.fail_before_dispatch(
                    operation.operation_id,
                    error_code="fixture_stop",
                )

            with LocalProgramOperator.open(database) as operator:
                before = tuple(
                    row["component"]
                    for row in operator._programs._db.execute(
                        "SELECT component FROM component_schema ORDER BY component"
                    ).fetchall()
                )
                audit = operator.audit_operation(operation.operation_id)
                self.assertEqual(
                    audit["operation"]["operation_id"],
                    operation.operation_id,
                )
                after = tuple(
                    row["component"]
                    for row in operator._programs._db.execute(
                        "SELECT component FROM component_schema ORDER BY component"
                    ).fetchall()
                )
                self.assertEqual(after, before)
                self.assertNotIn("actor_inference", after)
                self.assertNotIn("capability_registry", after)
                self.assertNotIn("authority", after)
                self.assertNotIn("evidence_store", after)
                self.assertNotIn("claim_store", after)
                self.assertNotIn("verification", after)

    def test_authority_audit_rejects_coherently_program_bound_decision_event(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "capital.db"
            decision_id = _create_ask(database)
            with ProgramRepository(database) as programs:
                row = programs._db.execute(
                    """
                    SELECT event_json FROM events
                    WHERE event_type = 'authority.decided'
                    ORDER BY sequence DESC LIMIT 1
                    """
                ).fetchone()
                event = record_from_json(Event, row["event_json"])
                rebound = _redigest_event(event, program_id="p-1")
                _write_event(programs, rebound)

            with LocalProgramOperator.open(database) as operator:
                with self.assertRaises(IntegrityViolation):
                    operator.asks("p-1")
                self.assertIsInstance(decision_id, str)

    def test_operation_audit_rejects_coherent_event_receipt_divergence(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "capital.db"
            with ProgramRepository(database) as programs:
                programs.create(Program("p-1", 0, "receipt provenance"))
                programs.transition("p-1", ProgramStatus.ACTIVE, expected_revision=0)
                journal = OperationJournal(programs)
                operation = journal.create_intent(
                    program_id="p-1",
                    actor_id="a-1",
                    resolution=_operation_resolution(),
                    authority_receipt_ref="authority-1",
                )
                journal.mark_admitted(operation.operation_id)
                journal.mark_running(operation.operation_id)
                journal.finish(
                    operation.operation_id,
                    ExecutionObservation(
                        ExecutionOutcome.SUCCEEDED,
                        EffectStatus.CONFIRMED,
                        {"bytes_written": 7},
                        backend_receipt_ref="backend-1",
                    ),
                )
                row = programs._db.execute(
                    """
                    SELECT event_json FROM events
                    WHERE event_type = 'operation.finished'
                      AND json_extract(event_json, '$.payload.operation.operation_id') = ?
                    """,
                    (operation.operation_id,),
                ).fetchone()
                event = record_from_json(Event, row["event_json"])
                payload = dict(event.payload.items())
                receipt_payload = payload["receipt"]
                self.assertIsInstance(receipt_payload, FrozenMap)
                receipt = record_from_json(
                    ExecutionReceipt,
                    __import__("ai_capital.kernel.serialization", fromlist=["canonical_json"]).canonical_json(
                        receipt_payload
                    ),
                )
                altered = replace(receipt, output={"tampered": True})
                payload["receipt"] = altered
                rewritten = _redigest_event(
                    event,
                    payload=to_canonical_data(payload),
                )
                _write_event(programs, rewritten)

            with LocalProgramOperator.open(database) as operator:
                with self.assertRaises(IntegrityViolation):
                    operator.audit_operation(operation.operation_id)

    def test_operation_audit_rejects_coherently_retyped_intermediate_event(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "capital.db"
            with ProgramRepository(database) as programs:
                programs.create(Program("p-1", 0, "event ordering"))
                programs.transition("p-1", ProgramStatus.ACTIVE, expected_revision=0)
                journal = OperationJournal(programs)
                operation = journal.create_intent(
                    program_id="p-1",
                    actor_id="a-1",
                    resolution=_operation_resolution(),
                    authority_receipt_ref="authority-1",
                )
                journal.mark_admitted(operation.operation_id)
                journal.mark_running(operation.operation_id)
                journal.finish(
                    operation.operation_id,
                    ExecutionObservation(
                        ExecutionOutcome.SUCCEEDED,
                        EffectStatus.CONFIRMED,
                        {},
                    ),
                )
                row = programs._db.execute(
                    """
                    SELECT event_json FROM events
                    WHERE event_type = 'operation.started'
                      AND json_extract(event_json, '$.payload.operation.operation_id') = ?
                    """,
                    (operation.operation_id,),
                ).fetchone()
                event = record_from_json(Event, row["event_json"])
                retyped = _redigest_event(event, event_type="operation.finished")
                _write_event(programs, retyped)

            with LocalProgramOperator.open(database) as operator:
                with self.assertRaises(IntegrityViolation):
                    operator.audit_operation(operation.operation_id)

    def test_operation_audit_accepts_supported_terminal_and_reconciliation_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "capital.db"
            with ProgramRepository(database) as programs:
                programs.create(Program("p-1", 0, "supported operation paths"))
                programs.transition("p-1", ProgramStatus.ACTIVE, expected_revision=0)
                journal = OperationJournal(programs)

                predispatch = journal.create_intent(
                    program_id="p-1",
                    actor_id="a-1",
                    resolution=_operation_resolution(),
                    authority_receipt_ref="authority-pre",
                )
                journal.fail_before_dispatch(
                    predispatch.operation_id,
                    error_code="pre_dispatch_fixture",
                )

                interrupted = journal.create_intent(
                    program_id="p-1",
                    actor_id="a-1",
                    resolution=replace(
                        _operation_resolution(),
                        request_id="req-interrupted",
                    ),
                    authority_receipt_ref="authority-interrupted",
                )
                journal.mark_admitted(interrupted.operation_id)
                journal.mark_running(interrupted.operation_id)
                recovered = journal.recover_interrupted()
                self.assertIn(
                    interrupted.operation_id,
                    {item.operation_id for item in recovered},
                )

                reconciled = journal.create_intent(
                    program_id="p-1",
                    actor_id="a-1",
                    resolution=replace(
                        _operation_resolution(),
                        request_id="req-reconciled",
                    ),
                    authority_receipt_ref="authority-reconciled",
                )
                journal.mark_admitted(reconciled.operation_id)
                journal.mark_running(reconciled.operation_id)
                journal.finish(
                    reconciled.operation_id,
                    ExecutionObservation(
                        ExecutionOutcome.TIMED_OUT,
                        EffectStatus.INDETERMINATE,
                        {},
                        error_code="timeout",
                    ),
                )
                journal.apply_reconciliation(
                    reconciled.operation_id,
                    ReconciliationObservation(
                        EffectStatus.INDETERMINATE,
                        "still_unknown",
                    ),
                )
                final = journal.apply_reconciliation(
                    reconciled.operation_id,
                    ReconciliationObservation(
                        EffectStatus.CONFIRMED,
                        "effect_confirmed",
                    ),
                )
                self.assertIs(
                    final.reconciliation_status,
                    ReconciliationStatus.RESOLVED,
                )

            with LocalProgramOperator.open(database) as operator:
                for operation_id in (
                    predispatch.operation_id,
                    interrupted.operation_id,
                    reconciled.operation_id,
                ):
                    audited = operator.audit_operation(operation_id)
                    self.assertEqual(
                        audited["operation"]["operation_id"],
                        operation_id,
                    )


if __name__ == "__main__":
    unittest.main()
