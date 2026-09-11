from __future__ import annotations

import sqlite3
from typing import Any

from ..kernel.actor_store import ActorRepository
from ..kernel.authority import ApprovalReceipt, AuthorityDecisionContext, AuthorityEngine
from ..kernel.authority_store import AuthorityRepository
from ..kernel.capability_store import CapabilityRepository
from ..kernel.claim_store import ClaimRepository
from ..kernel.durable_program import ProgramRepository
from ..kernel.enums import (
    ActorStatus,
    AuthorityDecisionKind,
    EffectStatus,
    ExecutionOutcome,
    ProgramStatus,
    ReconciliationStatus,
)
from ..kernel.errors import (
    ApprovalInvalid,
    AuthorityDenied,
    EvidenceMissing,
    IntegrityViolation,
    InvalidRequest,
    StaleActorGeneration,
    StaleCapabilityBinding,
    StaleProgramRevision,
    VerificationStale,
)
from ..kernel.events import utc_now, verify_event_digest
from ..kernel.evidence_store import EvidenceRepository
from ..kernel.frozen_json import FrozenMap
from ..kernel.models import (
    CapabilityResolution,
    Event,
    ExecutionAuthorityReceipt,
    Operation,
)
from ..kernel.operations import validate_operation_semantics
from ..kernel.operation_journal import (
    ExecutionReceipt,
    OperationJournal,
    ReconciliationReceipt,
)
from ..kernel.schema_codec import record_from_json
from ..kernel.serialization import canonical_digest, canonical_json, to_canonical_data
from ..kernel.verification import VerificationRepository


_OPERATION_EVENT_TYPES = (
    "operation.requested",
    "operation.admitted",
    "operation.started",
    "operation.finished",
    "operation.interrupted",
    "operation.reconciled",
)


class LocalAuditOperator:
    """Read-mostly product view over authenticated Host records and provenance."""

    def __init__(self, programs: ProgramRepository, operations: OperationJournal):
        if operations._host_store is not programs:
            raise InvalidRequest("audit components must share one Host store")
        self._programs = programs
        self._operations = operations
        self._actors: ActorRepository | None = None
        self._capabilities: CapabilityRepository | None = None
        self._authority_store: AuthorityRepository | None = None
        self._authority: AuthorityEngine | None = None
        self._evidence: EvidenceRepository | None = None
        self._claims: ClaimRepository | None = None
        self._verifications: VerificationRepository | None = None

    def _component_version(self, component: str) -> int | None:
        row = self._programs._db.execute(
            "SELECT version FROM component_schema WHERE component = ?",
            (component,),
        ).fetchone()
        return None if row is None else int(row["version"])

    def _authority_repository(
        self,
        *,
        required: bool,
    ) -> AuthorityRepository | None:
        if self._component_version("authority") is None:
            if required:
                raise InvalidRequest("Authority store is not initialized")
            return None
        if self._authority_store is None:
            self._authority_store = AuthorityRepository(self._programs)
        return self._authority_store

    def _authority_engine_components(self) -> AuthorityEngine:
        store = self._authority_repository(required=True)
        assert store is not None
        if self._component_version("actor_inference") is None:
            raise IntegrityViolation(
                "Authority state exists without the Actor component"
            )
        if self._component_version("capability_registry") is None:
            raise IntegrityViolation(
                "Authority state exists without the Capability component"
            )
        if self._authority is None:
            self._actors = ActorRepository(self._programs)
            self._capabilities = CapabilityRepository(self._programs)
            self._authority = AuthorityEngine(
                self._programs,
                self._actors,
                self._capabilities,
                store,
            )
        return self._authority

    def _evidence_repository(self) -> EvidenceRepository:
        if self._component_version("evidence_store") is None:
            raise EvidenceMissing("Evidence store is not initialized")
        if self._evidence is None:
            self._evidence = EvidenceRepository(self._programs)
        return self._evidence

    def _verification_repository(self) -> VerificationRepository:
        if self._component_version("verification") is None:
            raise InvalidRequest("Verification store is not initialized")
        if self._component_version("claim_store") is None:
            raise IntegrityViolation(
                "Verification state exists without the Claim component"
            )
        if self._component_version("evidence_store") is None:
            raise IntegrityViolation(
                "Verification state exists without the Evidence component"
            )
        if self._verifications is None:
            evidence = self._evidence_repository()
            self._claims = ClaimRepository(self._programs, evidence)
            self._verifications = VerificationRepository(self._programs, self._claims)
        return self._verifications

    @staticmethod
    def _event_anchor(event: Event) -> dict[str, Any]:
        return {
            "sequence": event.sequence,
            "event_id": event.event_id,
            "event_type": event.event_type,
            "occurred_at": event.occurred_at,
            "recorded_at": event.recorded_at,
            "actor_id": event.actor_id,
            "program_id": event.program_id,
            "correlation_id": event.correlation_id,
            "digest": event.digest,
        }

    @staticmethod
    def _decode_event_row(row: sqlite3.Row) -> Event:
        try:
            event = record_from_json(Event, row["event_json"])
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("audit Event cannot be decoded") from exc
        if not isinstance(event, Event):
            raise IntegrityViolation("audit Event decoded wrong type")
        if (
            event.sequence != int(row["sequence"])
            or event.event_id != row["event_id"]
            or event.program_id != row["program_id"]
            or event.event_type != row["event_type"]
            or event.digest != row["event_digest"]
            or not verify_event_digest(event)
        ):
            raise IntegrityViolation("audit Event row/digest binding mismatch")
        return event

    def _event_by_id(self, event_id: str) -> Event:
        row = self._programs._db.execute(
            """
            SELECT sequence, event_id, program_id, event_type, event_json, event_digest
            FROM events WHERE event_id = ?
            """,
            (event_id,),
        ).fetchone()
        if row is None:
            raise IntegrityViolation(f"audit provenance Event is missing: {event_id}")
        return self._decode_event_row(row)

    def _events_of_type(self, event_type: str) -> tuple[Event, ...]:
        rows = self._programs._db.execute(
            """
            SELECT sequence, event_id, program_id, event_type, event_json, event_digest
            FROM events WHERE event_type = ? ORDER BY sequence
            """,
            (event_type,),
        ).fetchall()
        return tuple(self._decode_event_row(row) for row in rows)

    @staticmethod
    def _event_context_matches(
        event: Event,
        *,
        actor_id: str | None = None,
        actorless: bool = False,
        correlation_id: str | None = None,
        uncorrelated: bool = False,
        host_scoped: bool = False,
    ) -> bool:
        if host_scoped and event.program_id is not None:
            return False
        if actorless and event.actor_id is not None:
            return False
        if actor_id is not None and event.actor_id != actor_id:
            return False
        if uncorrelated and event.correlation_id is not None:
            return False
        if correlation_id is not None and event.correlation_id != correlation_id:
            return False
        return True

    def _matching_event(
        self,
        event_type: str,
        payload: object,
        *,
        actor_id: str | None = None,
        actorless: bool = False,
        correlation_id: str | None = None,
        uncorrelated: bool = False,
        host_scoped: bool = False,
    ) -> Event:
        expected = to_canonical_data(payload)
        matches = [
            event
            for event in self._events_of_type(event_type)
            if to_canonical_data(event.payload) == expected
            and self._event_context_matches(
                event,
                actor_id=actor_id,
                actorless=actorless,
                correlation_id=correlation_id,
                uncorrelated=uncorrelated,
                host_scoped=host_scoped,
            )
        ]
        if len(matches) != 1:
            raise IntegrityViolation(
                f"audit provenance requires exactly one {event_type} Event"
            )
        return matches[0]

    def _matching_events(
        self,
        event_type: str,
        payload: object,
        *,
        actor_id: str | None = None,
        actorless: bool = False,
        correlation_id: str | None = None,
        uncorrelated: bool = False,
        host_scoped: bool = False,
    ) -> tuple[Event, ...]:
        expected = to_canonical_data(payload)
        return tuple(
            event
            for event in self._events_of_type(event_type)
            if to_canonical_data(event.payload) == expected
            and self._event_context_matches(
                event,
                actor_id=actor_id,
                actorless=actorless,
                correlation_id=correlation_id,
                uncorrelated=uncorrelated,
                host_scoped=host_scoped,
            )
        )

    def _approval_record(
        self,
        decision_id: str,
    ) -> tuple[ApprovalReceipt, str | None] | None:
        rows = self._programs._db.execute(
            """
            SELECT approval_id, decision_id, receipt_json, receipt_digest, consumed_at
            FROM approval_receipts WHERE decision_id = ? ORDER BY approval_id
            """,
            (decision_id,),
        ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise IntegrityViolation("AuthorityDecision has multiple approval receipts")
        row = rows[0]
        try:
            approval = record_from_json(ApprovalReceipt, row["receipt_json"])
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("approval audit record cannot be decoded") from exc
        if not isinstance(approval, ApprovalReceipt):
            raise IntegrityViolation("approval audit record decoded wrong type")
        if (
            approval.approval_id != row["approval_id"]
            or approval.decision_id != row["decision_id"]
            or canonical_digest(approval) != row["receipt_digest"]
        ):
            raise IntegrityViolation("approval audit row binding mismatch")
        consumed_at = row["consumed_at"]
        if consumed_at is not None and (
            type(consumed_at) is not str or not consumed_at.strip()
        ):
            raise IntegrityViolation("approval consumed timestamp is invalid")
        return approval, consumed_at

    @staticmethod
    def _validate_execution_receipt_binding(
        receipt: ExecutionAuthorityReceipt,
        context: AuthorityDecisionContext,
    ) -> None:
        if receipt.decision_id != context.decision.decision_id:
            raise IntegrityViolation("execution authority decision binding mismatch")
        if (
            receipt.program_id != context.program_id
            or receipt.program_revision != context.program_revision
            or receipt.actor_id != context.actor_id
            or receipt.actor_generation != context.actor_generation
            or receipt.capability_id != context.capability_id
            or receipt.capability_binding_revision != context.capability_binding_revision
            or receipt.policy_revision != context.decision.policy_revision
            or tuple(receipt.grant_refs) != tuple(context.decision.grant_refs)
            or receipt.resolved_effect_digest
            != canonical_digest(context.decision.resolved_effect)
        ):
            raise IntegrityViolation("execution authority context binding mismatch")

    def _execution_authority_record(
        self,
        context: AuthorityDecisionContext,
    ) -> tuple[ExecutionAuthorityReceipt, str | None] | None:
        store = self._authority_repository(required=True)
        assert store is not None
        rows = self._programs._db.execute(
            """
            SELECT receipt_id, single_use_identity, receipt_json, receipt_digest, consumed_at
            FROM execution_authority_receipts ORDER BY receipt_id
            """
        ).fetchall()
        matches: list[tuple[ExecutionAuthorityReceipt, str | None]] = []
        for row in rows:
            try:
                receipt = record_from_json(
                    ExecutionAuthorityReceipt,
                    row["receipt_json"],
                )
            except (TypeError, ValueError) as exc:
                raise IntegrityViolation(
                    "execution authority audit record cannot be decoded"
                ) from exc
            if not isinstance(receipt, ExecutionAuthorityReceipt):
                raise IntegrityViolation(
                    "execution authority audit record decoded wrong type"
                )
            if (
                receipt.receipt_id != row["receipt_id"]
                or receipt.single_use_identity != row["single_use_identity"]
                or canonical_digest(receipt) != row["receipt_digest"]
            ):
                raise IntegrityViolation("execution authority audit row binding mismatch")
            receipt_context = store.get_decision(receipt.decision_id)
            self._validate_execution_receipt_binding(receipt, receipt_context)
            consumed_at = row["consumed_at"]
            if consumed_at is not None and (
                type(consumed_at) is not str or not consumed_at.strip()
            ):
                raise IntegrityViolation(
                    "execution authority consumed timestamp is invalid"
                )
            if receipt.decision_id == context.decision.decision_id:
                matches.append((receipt, consumed_at))
        if len(matches) > 1:
            raise IntegrityViolation(
                "AuthorityDecision has multiple execution authority receipts"
            )
        return None if not matches else matches[0]

    def _validate_current_ask(self, context: AuthorityDecisionContext) -> None:
        engine = self._authority_engine_components()
        assert self._actors is not None
        assert self._capabilities is not None
        store = self._authority_repository(required=True)
        assert store is not None
        if context.decision.decision is not AuthorityDecisionKind.ASK:
            raise ApprovalInvalid("only an ask decision may receive approval")
        self._programs.verify_integrity(context.program_id)
        program = self._programs.get(context.program_id)
        actor = self._actors.get(context.actor_id)
        capability = self._capabilities.get(context.capability_id)
        policy = store.current_policy()
        if program.revision != context.program_revision:
            raise StaleProgramRevision("AuthorityDecision is stale for Program")
        if actor.generation != context.actor_generation:
            raise StaleActorGeneration("AuthorityDecision is stale for Actor")
        if capability.binding_revision != context.capability_binding_revision:
            raise StaleCapabilityBinding("AuthorityDecision is stale for Capability")
        if program.status is not ProgramStatus.ACTIVE:
            raise AuthorityDenied("approval requires an active Program")
        if actor.status is not ActorStatus.ACTIVE:
            raise AuthorityDenied("approval requires an active Actor")
        if policy.policy_revision != context.decision.policy_revision:
            raise ApprovalInvalid("approval request is stale for current policy")
        if context.resolution.binding_revision != capability.binding_revision:
            raise StaleCapabilityBinding("AuthorityDecision resolution is stale")
        if (
            context.resolution.resolved_effect.resource_type != capability.resource_type
            or context.resolution.resolved_effect.effect_class is not capability.effect_class
        ):
            raise IntegrityViolation(
                "AuthorityDecision resolution violates Capability contract"
            )
        current_grants = {
            grant.grant_id: grant
            for grant in store.active_grants(actor_id=context.actor_id)
        }
        engine._validate_decision_semantics(
            context=context,
            capability=capability,
            policy=policy,
            current_grants=current_grants,
            at=utc_now(),
        )

    def _ask_view(self, context: AuthorityDecisionContext) -> dict[str, Any]:
        decision_event = self._matching_event(
            "authority.decided",
            context,
            actor_id=context.actor_id,
            host_scoped=True,
            uncorrelated=True,
        )
        approval_record = self._approval_record(context.decision.decision_id)
        approval_view: dict[str, Any] | None = None
        approval_state = "awaiting_approval"
        approval_consumed_event: Event | None = None
        if approval_record is not None:
            approval, consumed_at = approval_record
            if (
                approval.resolved_effect_digest
                != canonical_digest(context.decision.resolved_effect)
                or approval.policy_revision != context.decision.policy_revision
            ):
                raise IntegrityViolation("approval does not match AuthorityDecision")
            issued = self._matching_event(
                "approval.issued",
                approval,
                actorless=True,
                host_scoped=True,
                uncorrelated=True,
            )
            if issued.sequence <= decision_event.sequence:
                raise IntegrityViolation(
                    "approval issuance precedes its AuthorityDecision"
                )
            consumed_events = self._matching_events(
                "approval.consumed",
                approval,
                actorless=True,
                host_scoped=True,
                uncorrelated=True,
            )
            if consumed_at is None:
                if consumed_events:
                    raise IntegrityViolation(
                        "unconsumed approval has a consumed semantic Event"
                    )
                approval_state = "approved"
                consumed_anchor = None
            else:
                if len(consumed_events) != 1:
                    raise IntegrityViolation(
                        "consumed approval lacks exactly one consumed semantic Event"
                    )
                approval_consumed_event = consumed_events[0]
                if approval_consumed_event.sequence <= issued.sequence:
                    raise IntegrityViolation(
                        "approval consumption precedes approval issuance"
                    )
                approval_state = "consumed"
                consumed_anchor = self._event_anchor(approval_consumed_event)
            approval_view = {
                "receipt": to_canonical_data(approval),
                "consumed_at": consumed_at,
                "issued_event": self._event_anchor(issued),
                "consumed_event": consumed_anchor,
            }

        execution_record = self._execution_authority_record(context)
        execution_view: dict[str, Any] | None = None
        if execution_record is not None:
            receipt, consumed_at = execution_record
            issued = self._matching_event(
                "authority.execution_issued",
                receipt,
                actorless=True,
                host_scoped=True,
                uncorrelated=True,
            )
            if approval_record is None or approval_record[1] is None:
                raise IntegrityViolation(
                    "ASK execution authority exists without a consumed approval"
                )
            assert approval_consumed_event is not None
            if issued.sequence <= approval_consumed_event.sequence:
                raise IntegrityViolation(
                    "execution authority issuance precedes approval consumption"
                )
            consumed_events = self._matching_events(
                "authority.execution_consumed",
                receipt,
                actorless=True,
                host_scoped=True,
                uncorrelated=True,
            )
            if consumed_at is None:
                if consumed_events:
                    raise IntegrityViolation(
                        "unconsumed execution authority has a consumed Event"
                    )
                consumed_anchor = None
            else:
                if len(consumed_events) != 1:
                    raise IntegrityViolation(
                        "consumed execution authority lacks exactly one consumed Event"
                    )
                consumed_event = consumed_events[0]
                if consumed_event.sequence <= issued.sequence:
                    raise IntegrityViolation(
                        "execution authority consumption precedes issuance"
                    )
                consumed_anchor = self._event_anchor(consumed_event)
            execution_view = {
                "receipt": to_canonical_data(receipt),
                "consumed_at": consumed_at,
                "issued_event": self._event_anchor(issued),
                "consumed_event": consumed_anchor,
            }

        currentness = "current"
        stale_reason = None
        try:
            self._validate_current_ask(context)
        except IntegrityViolation:
            raise
        except (
            ApprovalInvalid,
            AuthorityDenied,
            StaleProgramRevision,
            StaleActorGeneration,
            StaleCapabilityBinding,
        ) as exc:
            currentness = "stale"
            stale_reason = {
                "code": type(exc).__name__,
                "message": str(exc),
            }

        return {
            "decision": to_canonical_data(context.decision),
            "program_id": context.program_id,
            "program_revision": context.program_revision,
            "actor_id": context.actor_id,
            "actor_generation": context.actor_generation,
            "capability_id": context.capability_id,
            "capability_binding_revision": context.capability_binding_revision,
            "resolution": to_canonical_data(context.resolution),
            "approval_state": approval_state,
            "currentness": currentness,
            "stale_reason": stale_reason,
            "decision_event": self._event_anchor(decision_event),
            "approval": approval_view,
            "execution_authority": execution_view,
        }

    def asks(self, program_id: str) -> tuple[dict[str, Any], ...]:
        self._programs.verify_integrity(program_id)
        self._programs.get(program_id)
        store = self._authority_repository(required=False)
        if store is None:
            return ()
        rows = self._programs._db.execute(
            "SELECT decision_id FROM authority_decisions ORDER BY decision_id"
        ).fetchall()
        views: list[dict[str, Any]] = []
        for row in rows:
            context = store.get_decision(str(row["decision_id"]))
            if (
                context.program_id == program_id
                and context.decision.decision is AuthorityDecisionKind.ASK
            ):
                views.append(self._ask_view(context))
        views.sort(key=lambda item: int(item["decision_event"]["sequence"]))
        return tuple(views)

    def approve(self, decision_id: str) -> dict[str, Any]:
        store = self._authority_repository(required=True)
        assert store is not None
        context = store.get_decision(decision_id)
        self._validate_current_ask(context)
        if self._approval_record(decision_id) is not None:
            raise ApprovalInvalid("AuthorityDecision already has an approval receipt")
        engine = self._authority_engine_components()
        engine.approve(decision_id=decision_id)
        return self._ask_view(context)

    @staticmethod
    def _immutable_operation_identity(
        snapshot: Operation,
        current: Operation,
    ) -> bool:
        return (
            snapshot.operation_id == current.operation_id
            and snapshot.program_id == current.program_id
            and snapshot.actor_id == current.actor_id
            and snapshot.capability_id == current.capability_id
            and snapshot.authority_receipt_ref == current.authority_receipt_ref
            and snapshot.request_digest == current.request_digest
        )

    def _operation_receipts(
        self,
        operation: Operation,
    ) -> tuple[ExecutionReceipt | ReconciliationReceipt, ...]:
        rows = self._programs._db.execute(
            """
            SELECT sequence, receipt_id, operation_id, receipt_type,
                   receipt_json, receipt_digest
            FROM operation_receipts
            WHERE operation_id = ? ORDER BY sequence
            """,
            (operation.operation_id,),
        ).fetchall()
        receipts: list[ExecutionReceipt | ReconciliationReceipt] = []
        for row in rows:
            receipt_type = row["receipt_type"]
            cls = (
                ExecutionReceipt
                if receipt_type == "execution"
                else ReconciliationReceipt
                if receipt_type == "reconciliation"
                else None
            )
            if cls is None:
                raise IntegrityViolation("Operation audit found unknown receipt type")
            try:
                receipt = record_from_json(cls, row["receipt_json"])
            except (TypeError, ValueError) as exc:
                raise IntegrityViolation("Operation receipt cannot be decoded") from exc
            if not isinstance(receipt, cls):
                raise IntegrityViolation("Operation receipt decoded wrong type")
            if (
                receipt.receipt_id != row["receipt_id"]
                or receipt.operation_id != row["operation_id"]
                or receipt.operation_id != operation.operation_id
                or canonical_digest(receipt) != row["receipt_digest"]
            ):
                raise IntegrityViolation("Operation receipt row binding mismatch")
            receipts.append(receipt)
        if tuple(receipt.receipt_id for receipt in receipts) != operation.receipt_refs:
            raise IntegrityViolation("Operation receipt history diverges from projection")
        idempotency_key = self._operations.idempotency_key(operation.operation_id)
        for receipt in receipts:
            if (
                isinstance(receipt, ExecutionReceipt)
                and receipt.idempotency_key != idempotency_key
            ):
                raise IntegrityViolation(
                    "Operation execution receipt idempotency binding mismatch"
                )
        return tuple(receipts)

    def _operation_events(
        self,
        operation: Operation,
        resolution: CapabilityResolution,
        receipts: tuple[ExecutionReceipt | ReconciliationReceipt, ...],
    ) -> tuple[Event, ...]:
        placeholders = ",".join("?" for _ in _OPERATION_EVENT_TYPES)
        rows = self._programs._db.execute(
            f"""
            SELECT sequence, event_id, program_id, event_type, event_json, event_digest
            FROM events WHERE event_type IN ({placeholders}) ORDER BY sequence
            """,
            _OPERATION_EVENT_TYPES,
        ).fetchall()
        matches: list[Event] = []
        requested = 0
        admitted_sequences: list[int] = []
        seen_admitted = False
        previous_snapshot: Operation | None = None
        receipt_cursor = 0

        def exact_receipt(
            event_receipt: ExecutionReceipt | ReconciliationReceipt,
            expected_type: type[ExecutionReceipt] | type[ReconciliationReceipt],
        ) -> ExecutionReceipt | ReconciliationReceipt:
            nonlocal receipt_cursor
            if receipt_cursor >= len(receipts):
                raise IntegrityViolation(
                    "Operation semantic Event has an unrecorded receipt"
                )
            expected = receipts[receipt_cursor]
            if not isinstance(expected, expected_type) or event_receipt != expected:
                raise IntegrityViolation(
                    "Operation semantic Event receipt diverges from durable receipt"
                )
            receipt_cursor += 1
            return expected

        for row in rows:
            event = self._decode_event_row(row)
            payload = event.payload.get("operation")
            if not isinstance(payload, FrozenMap):
                raise IntegrityViolation("Operation semantic Event lacks Operation payload")
            try:
                snapshot = record_from_json(Operation, canonical_json(payload))
            except (TypeError, ValueError) as exc:
                raise IntegrityViolation("Operation Event snapshot cannot be decoded") from exc
            if not isinstance(snapshot, Operation):
                raise IntegrityViolation("Operation Event snapshot decoded wrong type")
            if snapshot.operation_id != operation.operation_id:
                continue
            if (
                event.actor_id != operation.actor_id
                or event.correlation_id != operation.program_id
                or event.program_id is not None
                or not self._immutable_operation_identity(snapshot, operation)
            ):
                raise IntegrityViolation("Operation Event context/identity binding mismatch")
            validate_operation_semantics(snapshot)

            receipt_payload = event.payload.get("receipt")
            event_receipt: ExecutionReceipt | ReconciliationReceipt | None = None
            if receipt_payload is not None:
                cls = (
                    ReconciliationReceipt
                    if event.event_type == "operation.reconciled"
                    else ExecutionReceipt
                )
                if not isinstance(receipt_payload, FrozenMap):
                    raise IntegrityViolation("Operation Event receipt payload is malformed")
                try:
                    event_receipt = record_from_json(
                        cls,
                        canonical_json(receipt_payload),
                    )
                except (TypeError, ValueError) as exc:
                    raise IntegrityViolation(
                        "Operation Event receipt cannot be decoded"
                    ) from exc
                if event_receipt.operation_id != operation.operation_id:
                    raise IntegrityViolation("Operation Event receipt binding mismatch")

            if event.event_type == "operation.requested":
                requested += 1
                if previous_snapshot is not None or event_receipt is not None:
                    raise IntegrityViolation(
                        "Operation requested Event is not the first semantic state"
                    )
                if (
                    snapshot.execution_outcome is not ExecutionOutcome.NOT_STARTED
                    or snapshot.effect_status is not EffectStatus.UNKNOWN
                    or snapshot.reconciliation_status
                    is not ReconciliationStatus.NOT_REQUIRED
                    or snapshot.started_at is not None
                    or snapshot.finished_at is not None
                    or snapshot.receipt_refs
                ):
                    raise IntegrityViolation(
                        "Operation requested Event contains a non-initial state"
                    )
                resolution_payload = event.payload.get("resolution")
                if not isinstance(resolution_payload, FrozenMap):
                    raise IntegrityViolation(
                        "requested Operation Event lacks CapabilityResolution"
                    )
                try:
                    requested_resolution = record_from_json(
                        CapabilityResolution,
                        canonical_json(resolution_payload),
                    )
                except (TypeError, ValueError) as exc:
                    raise IntegrityViolation(
                        "requested CapabilityResolution cannot be decoded"
                    ) from exc
                if requested_resolution != resolution:
                    raise IntegrityViolation(
                        "Operation projection resolution diverges from requested Event"
                    )

            elif event.event_type == "operation.admitted":
                if (
                    previous_snapshot is None
                    or seen_admitted
                    or snapshot != previous_snapshot
                    or snapshot.execution_outcome is not ExecutionOutcome.NOT_STARTED
                    or event_receipt is not None
                ):
                    raise IntegrityViolation(
                        "Operation admitted Event violates semantic ordering"
                    )
                seen_admitted = True
                admitted_sequences.append(event.sequence)
                if event.payload.get("authority_receipt_ref") != operation.authority_receipt_ref:
                    raise IntegrityViolation(
                        "Operation admission Event authority binding mismatch"
                    )

            elif event.event_type == "operation.started":
                if (
                    previous_snapshot is None
                    or not seen_admitted
                    or previous_snapshot.execution_outcome
                    is not ExecutionOutcome.NOT_STARTED
                    or snapshot.execution_outcome is not ExecutionOutcome.RUNNING
                    or snapshot.receipt_refs != previous_snapshot.receipt_refs
                    or snapshot.started_at is None
                    or snapshot.finished_at is not None
                    or event_receipt is not None
                ):
                    raise IntegrityViolation(
                        "Operation started Event violates semantic ordering"
                    )

            elif event.event_type in {"operation.finished", "operation.interrupted"}:
                if previous_snapshot is None or not isinstance(
                    event_receipt, ExecutionReceipt
                ):
                    raise IntegrityViolation(
                        "Operation terminal Event lacks an execution receipt"
                    )
                exact_receipt(event_receipt, ExecutionReceipt)
                if event.event_type == "operation.interrupted":
                    if (
                        previous_snapshot.execution_outcome
                        is not ExecutionOutcome.RUNNING
                        or snapshot.execution_outcome is not ExecutionOutcome.FAILED
                    ):
                        raise IntegrityViolation(
                            "Operation interrupted Event violates execution state"
                        )
                elif previous_snapshot.execution_outcome not in {
                    ExecutionOutcome.NOT_STARTED,
                    ExecutionOutcome.RUNNING,
                }:
                    raise IntegrityViolation(
                        "Operation finished Event follows a terminal state"
                    )
                if (
                    previous_snapshot.execution_outcome
                    is ExecutionOutcome.NOT_STARTED
                    and snapshot.execution_outcome is not ExecutionOutcome.FAILED
                ):
                    raise IntegrityViolation(
                        "pre-dispatch Operation finish must be a failure"
                    )
                expected_reconciliation = (
                    ReconciliationStatus.PENDING
                    if event_receipt.effect_status is EffectStatus.INDETERMINATE
                    else ReconciliationStatus.NOT_REQUIRED
                )
                if (
                    snapshot.execution_outcome is not event_receipt.execution_outcome
                    or snapshot.effect_status is not event_receipt.effect_status
                    or snapshot.reconciliation_status is not expected_reconciliation
                    or snapshot.finished_at != event_receipt.observed_at
                    or snapshot.receipt_refs
                    != previous_snapshot.receipt_refs + (event_receipt.receipt_id,)
                    or (
                        previous_snapshot.execution_outcome
                        is ExecutionOutcome.RUNNING
                        and snapshot.started_at != previous_snapshot.started_at
                    )
                    or (
                        previous_snapshot.execution_outcome
                        is ExecutionOutcome.NOT_STARTED
                        and snapshot.started_at is not None
                    )
                ):
                    raise IntegrityViolation(
                        "Operation terminal Event disagrees with execution receipt"
                    )

            elif event.event_type == "operation.reconciled":
                if previous_snapshot is None or not isinstance(
                    event_receipt, ReconciliationReceipt
                ):
                    raise IntegrityViolation(
                        "Operation reconciliation Event lacks a reconciliation receipt"
                    )
                exact_receipt(event_receipt, ReconciliationReceipt)
                if (
                    previous_snapshot.execution_outcome
                    in {ExecutionOutcome.NOT_STARTED, ExecutionOutcome.RUNNING}
                    or previous_snapshot.effect_status is not EffectStatus.INDETERMINATE
                    or previous_snapshot.reconciliation_status
                    not in {
                        ReconciliationStatus.PENDING,
                        ReconciliationStatus.UNRESOLVED,
                    }
                ):
                    raise IntegrityViolation(
                        "Operation reconciliation Event follows a closed state"
                    )
                expected_reconciliation = (
                    ReconciliationStatus.UNRESOLVED
                    if event_receipt.effect_status is EffectStatus.INDETERMINATE
                    else ReconciliationStatus.RESOLVED
                )
                if (
                    snapshot.execution_outcome
                    is not previous_snapshot.execution_outcome
                    or snapshot.started_at != previous_snapshot.started_at
                    or snapshot.finished_at != previous_snapshot.finished_at
                    or snapshot.effect_status is not event_receipt.effect_status
                    or snapshot.reconciliation_status is not expected_reconciliation
                    or snapshot.receipt_refs
                    != previous_snapshot.receipt_refs + (event_receipt.receipt_id,)
                ):
                    raise IntegrityViolation(
                        "Operation reconciliation Event disagrees with receipt"
                    )

            matches.append(event)
            previous_snapshot = snapshot

        if requested != 1 or not matches:
            raise IntegrityViolation(
                "Operation audit requires exactly one requested semantic Event"
            )
        if previous_snapshot != operation:
            raise IntegrityViolation(
                "Operation projection diverges from latest semantic Event"
            )
        if receipt_cursor != len(receipts):
            raise IntegrityViolation(
                "Operation durable receipt lacks matching semantic Event"
            )
        projection = self._programs._db.execute(
            """
            SELECT admitted_sequence, last_sequence FROM operation_projections
            WHERE operation_id = ?
            """,
            (operation.operation_id,),
        ).fetchone()
        if projection is None:
            raise IntegrityViolation("Operation projection disappeared during audit")
        if int(projection["last_sequence"]) != matches[-1].sequence:
            raise IntegrityViolation("Operation last-sequence provenance mismatch")
        admitted_sequence = projection["admitted_sequence"]
        if admitted_sequence is None:
            if admitted_sequences:
                raise IntegrityViolation(
                    "Operation admission Event lacks projection anchor"
                )
        elif admitted_sequences != [int(admitted_sequence)]:
            raise IntegrityViolation(
                "Operation admission sequence provenance mismatch"
            )
        return tuple(matches)

    def audit_operation(self, operation_id: str) -> dict[str, Any]:
        operation = self._operations.get(operation_id)
        resolution = self._operations.resolution(operation_id)
        receipts = self._operation_receipts(operation)
        events = self._operation_events(operation, resolution, receipts)
        return {
            "operation": to_canonical_data(operation),
            "resolution": to_canonical_data(resolution),
            "idempotency_key": self._operations.idempotency_key(operation_id),
            "receipts": tuple(to_canonical_data(receipt) for receipt in receipts),
            "events": tuple(self._event_anchor(event) for event in events),
        }

    def audit_evidence(self, evidence_id: str) -> dict[str, Any]:
        repository = self._evidence_repository()
        evidence = repository.get(evidence_id)
        admission = repository.admission(evidence_id)
        row = self._programs._db.execute(
            """
            SELECT admitted_event_id, artifact_digest
            FROM evidence_records WHERE evidence_id = ?
            """,
            (evidence_id,),
        ).fetchone()
        if row is None:
            raise IntegrityViolation("Evidence audit record is missing")
        artifact = self._programs._db.execute(
            """
            SELECT content_ref, byte_length FROM evidence_artifacts
            WHERE artifact_digest = ?
            """,
            (evidence.digest,),
        ).fetchone()
        if artifact is None:
            raise IntegrityViolation("Evidence artifact audit metadata is missing")
        event = self._event_by_id(str(row["admitted_event_id"]))
        if (
            event.event_type != "evidence.admitted"
            or event.program_id is not None
            or event.actor_id is not None
            or event.correlation_id != evidence.evidence_id
            or to_canonical_data(event.payload)
            != to_canonical_data({"evidence": evidence, "admission": admission})
            or row["artifact_digest"] != evidence.digest
            or artifact["content_ref"] != evidence.content_ref
        ):
            raise IntegrityViolation("Evidence audit provenance binding mismatch")
        return {
            "evidence": to_canonical_data(evidence),
            "admission": to_canonical_data(admission),
            "artifact": {
                "content_ref": artifact["content_ref"],
                "digest": evidence.digest,
                "byte_length": int(artifact["byte_length"]),
            },
            "event": self._event_anchor(event),
        }

    def audit_verification(self, verification_id: str) -> dict[str, Any]:
        repository = self._verification_repository()
        verification = repository.get(verification_id)
        contract = repository.contract(verification.contract_ref)
        row = self._programs._db.execute(
            """
            SELECT event_sequence, event_id, rationale_code
            FROM verification_receipts WHERE verification_id = ?
            """,
            (verification_id,),
        ).fetchone()
        if row is None:
            raise IntegrityViolation("Verification audit receipt is missing")
        event = self._event_by_id(str(row["event_id"]))
        if (
            event.sequence != int(row["event_sequence"])
            or event.event_type != "verification.recorded"
            or event.program_id is not None
            or event.actor_id is not None
            or event.correlation_id != contract.program_id
            or to_canonical_data(event.payload.get("verification"))
            != to_canonical_data(verification)
            or event.payload.get("rationale_code") != row["rationale_code"]
        ):
            raise IntegrityViolation("Verification audit provenance binding mismatch")
        currentness = "current"
        stale_reason = None
        try:
            repository.current(verification_id)
        except VerificationStale as exc:
            currentness = "stale"
            stale_reason = str(exc)
        return {
            "verification": to_canonical_data(verification),
            "contract": to_canonical_data(contract),
            "rationale_code": row["rationale_code"],
            "currentness": currentness,
            "stale_reason": stale_reason,
            "event": self._event_anchor(event),
        }
