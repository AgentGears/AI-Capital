from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace
from typing import Protocol
from uuid import uuid4

from .authority import AuthorityEngine
from .durable_program import ProgramRepository
from .enums import (
    EffectClass,
    EffectStatus,
    ExecutionOutcome,
    ReconciliationStatus,
)
from .errors import (
    ExecutionCancelled,
    ExecutionFailure,
    ExecutionTimeout,
    IntegrityViolation,
    InvalidRequest,
    PersistenceConflict,
    ReconciliationRequired,
)
from .events import event_digest_fields, utc_now
from .frozen_json import FrozenMap, freeze_json
from .models import CapabilityResolution, Event, Operation, ResolvedEffect
from .operations import retry_is_safe, validate_operation_semantics
from .schema_codec import record_from_json, record_to_json
from .serialization import canonical_digest, to_canonical_data


_COMPONENT = "operation_journal"
_COMPONENT_SCHEMA_VERSION = 2


@dataclass(frozen=True, slots=True)
class ExecutionObservation:
    execution_outcome: ExecutionOutcome
    effect_status: EffectStatus
    output: FrozenMap
    backend_receipt_ref: str | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        frozen = freeze_json(self.output)
        if not isinstance(frozen, FrozenMap):
            raise TypeError("execution output must be an object")
        object.__setattr__(self, "output", frozen)


@dataclass(frozen=True, slots=True)
class ExecutionReceipt:
    receipt_id: str
    operation_id: str
    execution_outcome: ExecutionOutcome
    effect_status: EffectStatus
    output: FrozenMap
    backend_receipt_ref: str | None
    error_code: str | None
    observed_at: str
    idempotency_key: str | None

    def __post_init__(self) -> None:
        frozen = freeze_json(self.output)
        if not isinstance(frozen, FrozenMap):
            raise TypeError("execution receipt output must be an object")
        object.__setattr__(self, "output", frozen)


@dataclass(frozen=True, slots=True)
class ReconciliationObservation:
    effect_status: EffectStatus
    rationale_code: str
    evidence_refs: tuple[str, ...] = ()
    backend_receipt_ref: str | None = None


@dataclass(frozen=True, slots=True)
class ReconciliationReceipt:
    receipt_id: str
    operation_id: str
    effect_status: EffectStatus
    rationale_code: str
    evidence_refs: tuple[str, ...]
    backend_receipt_ref: str | None
    reconciled_at: str


class EffectExecutor(Protocol):
    supports_idempotency: bool

    def execute(
        self,
        effect: ResolvedEffect,
        *,
        idempotency_key: str | None,
    ) -> ExecutionObservation: ...


class EffectReconciler(Protocol):
    def reconcile(
        self,
        effect: ResolvedEffect,
        *,
        execution_receipt: ExecutionReceipt,
        idempotency_key: str | None,
    ) -> ReconciliationObservation: ...


class OperationJournal:
    """Host-owned Operation truth, receipts, and reconciliation state."""

    def __init__(self, host_store: ProgramRepository):
        self._host_store = host_store
        self._migrate()

    def _migrate(self) -> None:
        with self._host_store._transaction():
            self._host_store._db.execute(
                """
                CREATE TABLE IF NOT EXISTS component_schema (
                    component TEXT PRIMARY KEY,
                    version INTEGER NOT NULL
                )
                """
            )
            row = self._host_store._db.execute(
                "SELECT version FROM component_schema WHERE component = ?",
                (_COMPONENT,),
            ).fetchone()
            version = None if row is None else int(row[0])
            if version is not None and version > _COMPONENT_SCHEMA_VERSION:
                raise IntegrityViolation(
                    f"Operation schema version {version} is newer than supported "
                    f"{_COMPONENT_SCHEMA_VERSION}"
                )

            if version is None:
                self._host_store._db.execute(
                    """
                    CREATE TABLE operation_projections (
                        operation_id TEXT PRIMARY KEY,
                        program_id TEXT NOT NULL,
                        actor_id TEXT NOT NULL,
                        capability_id TEXT NOT NULL,
                        authority_receipt_ref TEXT NOT NULL,
                        operation_json TEXT NOT NULL,
                        operation_digest TEXT NOT NULL,
                        resolution_json TEXT NOT NULL,
                        resolution_digest TEXT NOT NULL,
                        idempotency_key TEXT,
                        admitted_sequence INTEGER,
                        last_sequence INTEGER NOT NULL
                    )
                    """
                )
                self._host_store._db.execute(
                    """
                    CREATE INDEX operations_program
                        ON operation_projections(program_id, operation_id)
                    """
                )
                self._host_store._db.execute(
                    """
                    CREATE TABLE operation_receipts (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        receipt_id TEXT NOT NULL UNIQUE,
                        operation_id TEXT NOT NULL,
                        receipt_type TEXT NOT NULL,
                        receipt_json TEXT NOT NULL,
                        receipt_digest TEXT NOT NULL
                    )
                    """
                )
                self._host_store._db.execute(
                    """
                    CREATE INDEX operation_receipts_operation
                        ON operation_receipts(operation_id, sequence)
                    """
                )
                self._host_store._db.execute(
                    "INSERT INTO component_schema(component, version) VALUES (?, ?)",
                    (_COMPONENT, _COMPONENT_SCHEMA_VERSION),
                )
                return

            if version == 1:
                columns = {
                    str(item[1])
                    for item in self._host_store._db.execute(
                        "PRAGMA table_info(operation_projections)"
                    ).fetchall()
                }
                if "admitted_sequence" not in columns:
                    self._host_store._db.execute(
                        "ALTER TABLE operation_projections ADD COLUMN admitted_sequence INTEGER"
                    )
                self._host_store._db.execute(
                    "UPDATE component_schema SET version = ? WHERE component = ?",
                    (_COMPONENT_SCHEMA_VERSION, _COMPONENT),
                )
                version = _COMPONENT_SCHEMA_VERSION

            if version != _COMPONENT_SCHEMA_VERSION:
                raise IntegrityViolation(f"unsupported Operation schema version {version}")

    def _append_event(
        self,
        event_type: str,
        payload: object,
        *,
        program_id: str,
        actor_id: str,
    ) -> Event:
        sequence = self._host_store._next_sequence()
        event_id = str(uuid4())
        occurred_at = utc_now()
        recorded_at = utc_now()
        canonical_payload = to_canonical_data(payload)
        digest = event_digest_fields(
            event_id=event_id,
            sequence=sequence,
            event_type=event_type,
            occurred_at=occurred_at,
            recorded_at=recorded_at,
            payload=canonical_payload,
            actor_id=actor_id,
            program_id=None,
            causation_id=None,
            correlation_id=program_id,
        )
        event = Event(
            event_id=event_id,
            sequence=sequence,
            event_type=event_type,
            occurred_at=occurred_at,
            recorded_at=recorded_at,
            payload=canonical_payload,
            digest=digest,
            actor_id=actor_id,
            program_id=None,
            correlation_id=program_id,
        )
        self._host_store._insert_event(event)
        return event

    def _read_row(self, operation_id: str) -> sqlite3.Row:
        row = self._host_store._db.execute(
            """
            SELECT operation_id, program_id, actor_id, capability_id,
                   authority_receipt_ref, operation_json, operation_digest,
                   resolution_json, resolution_digest, idempotency_key,
                   admitted_sequence, last_sequence
            FROM operation_projections WHERE operation_id = ?
            """,
            (operation_id,),
        ).fetchone()
        if row is None:
            raise InvalidRequest(f"unknown Operation: {operation_id}")
        return row

    def _decode_row(self, row: sqlite3.Row) -> tuple[Operation, CapabilityResolution]:
        try:
            operation = record_from_json(Operation, row["operation_json"])
            resolution = record_from_json(CapabilityResolution, row["resolution_json"])
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("Operation projection cannot be decoded") from exc
        if not isinstance(operation, Operation) or not isinstance(
            resolution, CapabilityResolution
        ):
            raise IntegrityViolation("Operation projection decoded wrong record type")
        if operation.operation_id != row["operation_id"]:
            raise IntegrityViolation("Operation row identity mismatch")
        if operation.program_id != row["program_id"]:
            raise IntegrityViolation("Operation row Program mismatch")
        if operation.actor_id != row["actor_id"]:
            raise IntegrityViolation("Operation row Actor mismatch")
        if operation.capability_id != row["capability_id"]:
            raise IntegrityViolation("Operation row Capability mismatch")
        if operation.authority_receipt_ref != row["authority_receipt_ref"]:
            raise IntegrityViolation("Operation row authority receipt mismatch")
        if canonical_digest(operation) != row["operation_digest"]:
            raise IntegrityViolation("Operation digest mismatch")
        if canonical_digest(resolution) != row["resolution_digest"]:
            raise IntegrityViolation("Operation resolution digest mismatch")
        if operation.request_digest != canonical_digest(resolution):
            raise IntegrityViolation("Operation request digest differs from resolution")
        if operation.capability_id != resolution.capability_id:
            raise IntegrityViolation("Operation Capability differs from resolution")
        validate_operation_semantics(operation)
        return operation, resolution

    def get(self, operation_id: str) -> Operation:
        operation, _ = self._decode_row(self._read_row(operation_id))
        return operation

    def resolution(self, operation_id: str) -> CapabilityResolution:
        _, resolution = self._decode_row(self._read_row(operation_id))
        return resolution

    def idempotency_key(self, operation_id: str) -> str | None:
        row = self._read_row(operation_id)
        self._decode_row(row)
        return row["idempotency_key"]

    def create_intent(
        self,
        *,
        program_id: str,
        actor_id: str,
        resolution: CapabilityResolution,
        authority_receipt_ref: str,
        idempotency_key: str | None = None,
    ) -> Operation:
        if not program_id.strip() or not actor_id.strip():
            raise InvalidRequest("Operation Program and Actor identities must be non-empty")
        if not authority_receipt_ref.strip():
            raise InvalidRequest("Operation requires an execution-authority receipt")
        if idempotency_key is not None and not idempotency_key.strip():
            raise InvalidRequest("idempotency key must be non-empty when supplied")
        operation = Operation(
            operation_id=str(uuid4()),
            program_id=program_id,
            actor_id=actor_id,
            capability_id=resolution.capability_id,
            authority_receipt_ref=authority_receipt_ref,
            request_digest=canonical_digest(resolution),
            execution_outcome=ExecutionOutcome.NOT_STARTED,
            effect_status=EffectStatus.UNKNOWN,
            reconciliation_status=ReconciliationStatus.NOT_REQUIRED,
        )
        validate_operation_semantics(operation)
        try:
            with self._host_store._transaction():
                event = self._append_event(
                    "operation.requested",
                    {"operation": operation, "resolution": resolution},
                    program_id=program_id,
                    actor_id=actor_id,
                )
                self._host_store._db.execute(
                    """
                    INSERT INTO operation_projections(
                        operation_id, program_id, actor_id, capability_id,
                        authority_receipt_ref, operation_json, operation_digest,
                        resolution_json, resolution_digest, idempotency_key,
                        admitted_sequence, last_sequence
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                    """,
                    (
                        operation.operation_id,
                        operation.program_id,
                        operation.actor_id,
                        operation.capability_id,
                        operation.authority_receipt_ref,
                        record_to_json(operation),
                        canonical_digest(operation),
                        record_to_json(resolution),
                        canonical_digest(resolution),
                        idempotency_key,
                        event.sequence,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise PersistenceConflict("Operation identity collision") from exc
        return operation

    def mark_admitted(self, operation_id: str) -> Operation:
        row = self._read_row(operation_id)
        current, _ = self._decode_row(row)
        if current.execution_outcome is not ExecutionOutcome.NOT_STARTED:
            raise IntegrityViolation("only a not-started Operation may be admitted")
        if row["admitted_sequence"] is not None:
            raise IntegrityViolation("Operation is already admitted")
        with self._host_store._transaction():
            row = self._read_row(operation_id)
            current, _ = self._decode_row(row)
            if current.execution_outcome is not ExecutionOutcome.NOT_STARTED:
                raise PersistenceConflict("Operation changed before admission")
            if row["admitted_sequence"] is not None:
                raise IntegrityViolation("Operation is already admitted")
            event = self._append_event(
                "operation.admitted",
                {
                    "operation": current,
                    "authority_receipt_ref": current.authority_receipt_ref,
                },
                program_id=current.program_id,
                actor_id=current.actor_id,
            )
            cursor = self._host_store._db.execute(
                """
                UPDATE operation_projections
                SET admitted_sequence = ?, last_sequence = ?
                WHERE operation_id = ? AND operation_digest = ?
                  AND admitted_sequence IS NULL
                """,
                (
                    event.sequence,
                    event.sequence,
                    operation_id,
                    canonical_digest(current),
                ),
            )
            if cursor.rowcount != 1:
                raise PersistenceConflict("Operation changed during admission")
        return current

    def _insert_receipt(
        self,
        *,
        receipt_id: str,
        operation_id: str,
        receipt_type: str,
        receipt: object,
    ) -> None:
        self._host_store._db.execute(
            """
            INSERT INTO operation_receipts(
                receipt_id, operation_id, receipt_type, receipt_json, receipt_digest
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                receipt_id,
                operation_id,
                receipt_type,
                record_to_json(receipt),
                canonical_digest(receipt),
            ),
        )

    def _commit_projection(
        self,
        *,
        previous: Operation,
        updated: Operation,
        event_type: str,
        receipt: object | None = None,
        receipt_id: str | None = None,
        receipt_type: str | None = None,
    ) -> Operation:
        validate_operation_semantics(updated)
        if previous.operation_id != updated.operation_id:
            raise IntegrityViolation("Operation mutation changed identity")
        with self._host_store._transaction():
            current, _ = self._decode_row(self._read_row(previous.operation_id))
            if current != previous:
                raise PersistenceConflict("Operation changed before journal commit")
            if receipt is not None:
                if receipt_id is None or receipt_type is None:
                    raise IntegrityViolation("Operation receipt metadata is incomplete")
                self._insert_receipt(
                    receipt_id=receipt_id,
                    operation_id=updated.operation_id,
                    receipt_type=receipt_type,
                    receipt=receipt,
                )
            event = self._append_event(
                event_type,
                {"operation": updated, "receipt": receipt},
                program_id=updated.program_id,
                actor_id=updated.actor_id,
            )
            cursor = self._host_store._db.execute(
                """
                UPDATE operation_projections
                SET operation_json = ?, operation_digest = ?, last_sequence = ?
                WHERE operation_id = ? AND operation_digest = ?
                """,
                (
                    record_to_json(updated),
                    canonical_digest(updated),
                    event.sequence,
                    previous.operation_id,
                    canonical_digest(previous),
                ),
            )
            if cursor.rowcount != 1:
                raise PersistenceConflict("Operation projection changed during commit")
        return updated

    def mark_running(self, operation_id: str) -> Operation:
        row = self._read_row(operation_id)
        current, _ = self._decode_row(row)
        if current.execution_outcome is not ExecutionOutcome.NOT_STARTED:
            raise IntegrityViolation("only a not-started Operation may begin execution")
        if row["admitted_sequence"] is None:
            raise IntegrityViolation("Operation cannot start before admission")
        updated = replace(
            current,
            execution_outcome=ExecutionOutcome.RUNNING,
            started_at=utc_now(),
        )
        return self._commit_projection(
            previous=current,
            updated=updated,
            event_type="operation.started",
        )

    def _execution_receipt(
        self,
        operation: Operation,
        observation: ExecutionObservation,
    ) -> ExecutionReceipt:
        return ExecutionReceipt(
            receipt_id=str(uuid4()),
            operation_id=operation.operation_id,
            execution_outcome=observation.execution_outcome,
            effect_status=observation.effect_status,
            output=observation.output,
            backend_receipt_ref=observation.backend_receipt_ref,
            error_code=observation.error_code,
            observed_at=utc_now(),
            idempotency_key=self.idempotency_key(operation.operation_id),
        )

    def fail_before_dispatch(self, operation_id: str, *, error_code: str) -> Operation:
        current = self.get(operation_id)
        if current.execution_outcome is not ExecutionOutcome.NOT_STARTED:
            raise IntegrityViolation("pre-dispatch failure requires a not-started Operation")
        resolution = self.resolution(operation_id)
        effect_status = (
            EffectStatus.NOT_APPLICABLE
            if resolution.resolved_effect.effect_class is EffectClass.OBSERVE
            else EffectStatus.ABSENT
        )
        observation = ExecutionObservation(
            execution_outcome=ExecutionOutcome.FAILED,
            effect_status=effect_status,
            output={},
            error_code=error_code,
        )
        receipt = self._execution_receipt(current, observation)
        updated = replace(
            current,
            execution_outcome=ExecutionOutcome.FAILED,
            effect_status=effect_status,
            reconciliation_status=ReconciliationStatus.NOT_REQUIRED,
            finished_at=receipt.observed_at,
            receipt_refs=current.receipt_refs + (receipt.receipt_id,),
        )
        return self._commit_projection(
            previous=current,
            updated=updated,
            event_type="operation.finished",
            receipt=receipt,
            receipt_id=receipt.receipt_id,
            receipt_type="execution",
        )

    @staticmethod
    def _reconciliation_status(effect_status: EffectStatus) -> ReconciliationStatus:
        if effect_status is EffectStatus.INDETERMINATE:
            return ReconciliationStatus.PENDING
        return ReconciliationStatus.NOT_REQUIRED

    @staticmethod
    def _validate_effect_dimension(
        effect_class: EffectClass,
        effect_status: EffectStatus,
    ) -> None:
        if effect_class is EffectClass.OBSERVE:
            if effect_status is not EffectStatus.NOT_APPLICABLE:
                raise IntegrityViolation(
                    "observational execution must use not_applicable effect status"
                )
            return
        if effect_status is EffectStatus.NOT_APPLICABLE:
            raise IntegrityViolation(
                "mutating execution cannot use not_applicable effect status"
            )

    def finish(
        self,
        operation_id: str,
        observation: ExecutionObservation,
    ) -> Operation:
        current = self.get(operation_id)
        if current.execution_outcome is not ExecutionOutcome.RUNNING:
            raise IntegrityViolation("only a running Operation may finish execution")
        if observation.execution_outcome in {
            ExecutionOutcome.NOT_STARTED,
            ExecutionOutcome.RUNNING,
        }:
            raise IntegrityViolation("execution observation must be terminal")
        resolution = self.resolution(operation_id)
        self._validate_effect_dimension(
            resolution.resolved_effect.effect_class,
            observation.effect_status,
        )
        if (
            observation.execution_outcome is ExecutionOutcome.SUCCEEDED
            and observation.error_code is not None
        ):
            raise IntegrityViolation("successful execution cannot carry an error code")
        updated = replace(
            current,
            execution_outcome=observation.execution_outcome,
            effect_status=observation.effect_status,
            reconciliation_status=self._reconciliation_status(observation.effect_status),
            finished_at=utc_now(),
        )
        validate_operation_semantics(updated)
        receipt = self._execution_receipt(updated, observation)
        updated = replace(
            updated,
            finished_at=receipt.observed_at,
            receipt_refs=current.receipt_refs + (receipt.receipt_id,),
        )
        return self._commit_projection(
            previous=current,
            updated=updated,
            event_type="operation.finished",
            receipt=receipt,
            receipt_id=receipt.receipt_id,
            receipt_type="execution",
        )

    def execution_receipt(self, operation_id: str) -> ExecutionReceipt:
        operation = self.get(operation_id)
        row = self._host_store._db.execute(
            """
            SELECT receipt_id, operation_id, receipt_type, receipt_json, receipt_digest
            FROM operation_receipts
            WHERE operation_id = ? AND receipt_type = 'execution'
            ORDER BY sequence DESC LIMIT 1
            """,
            (operation_id,),
        ).fetchone()
        if row is None:
            raise InvalidRequest(f"Operation has no execution receipt: {operation_id}")
        try:
            receipt = record_from_json(ExecutionReceipt, row["receipt_json"])
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("execution receipt cannot be decoded") from exc
        if receipt.receipt_id != row["receipt_id"]:
            raise IntegrityViolation("execution receipt row identity mismatch")
        if receipt.operation_id != row["operation_id"] or row["receipt_type"] != "execution":
            raise IntegrityViolation("execution receipt row binding mismatch")
        if canonical_digest(receipt) != row["receipt_digest"]:
            raise IntegrityViolation("execution receipt digest mismatch")
        if receipt.operation_id != operation_id:
            raise IntegrityViolation("execution receipt Operation mismatch")
        if receipt.receipt_id not in operation.receipt_refs:
            raise IntegrityViolation("execution receipt is not linked from Operation")
        return receipt

    def recover_interrupted(self) -> tuple[Operation, ...]:
        rows = self._host_store._db.execute(
            """
            SELECT operation_id, admitted_sequence FROM operation_projections
            ORDER BY operation_id
            """
        ).fetchall()
        recovered: list[Operation] = []
        for row in rows:
            current = self.get(row["operation_id"])
            if current.execution_outcome is ExecutionOutcome.NOT_STARTED:
                error_code = (
                    "host_interrupted_after_admission_before_dispatch"
                    if row["admitted_sequence"] is not None
                    else "host_interrupted_before_admission"
                )
                recovered.append(
                    self.fail_before_dispatch(
                        current.operation_id,
                        error_code=error_code,
                    )
                )
                continue
            if current.execution_outcome is not ExecutionOutcome.RUNNING:
                continue
            resolution = self.resolution(current.operation_id)
            effect_status = (
                EffectStatus.NOT_APPLICABLE
                if resolution.resolved_effect.effect_class is EffectClass.OBSERVE
                else EffectStatus.INDETERMINATE
            )
            reconciliation_status = (
                ReconciliationStatus.NOT_REQUIRED
                if effect_status is EffectStatus.NOT_APPLICABLE
                else ReconciliationStatus.PENDING
            )
            observation = ExecutionObservation(
                execution_outcome=ExecutionOutcome.FAILED,
                effect_status=effect_status,
                output={},
                error_code="host_interrupted_after_dispatch_boundary",
            )
            receipt = self._execution_receipt(current, observation)
            updated = replace(
                current,
                execution_outcome=ExecutionOutcome.FAILED,
                effect_status=effect_status,
                reconciliation_status=reconciliation_status,
                finished_at=receipt.observed_at,
                receipt_refs=current.receipt_refs + (receipt.receipt_id,),
            )
            recovered.append(
                self._commit_projection(
                    previous=current,
                    updated=updated,
                    event_type="operation.interrupted",
                    receipt=receipt,
                    receipt_id=receipt.receipt_id,
                    receipt_type="execution",
                )
            )
        return tuple(recovered)

    def apply_reconciliation(
        self,
        operation_id: str,
        observation: ReconciliationObservation,
    ) -> Operation:
        current = self.get(operation_id)
        if current.effect_status is not EffectStatus.INDETERMINATE:
            raise ReconciliationRequired(
                "only an indeterminate Operation may be reconciled"
            )
        if current.reconciliation_status not in {
            ReconciliationStatus.PENDING,
            ReconciliationStatus.UNRESOLVED,
        }:
            raise IntegrityViolation("Operation reconciliation state is not open")
        if observation.effect_status not in {
            EffectStatus.CONFIRMED,
            EffectStatus.ABSENT,
            EffectStatus.INDETERMINATE,
        }:
            raise IntegrityViolation("reconciliation returned an invalid effect status")
        if not observation.rationale_code.strip():
            raise IntegrityViolation("reconciliation requires a rationale code")
        next_reconciliation = (
            ReconciliationStatus.UNRESOLVED
            if observation.effect_status is EffectStatus.INDETERMINATE
            else ReconciliationStatus.RESOLVED
        )
        receipt = ReconciliationReceipt(
            receipt_id=str(uuid4()),
            operation_id=operation_id,
            effect_status=observation.effect_status,
            rationale_code=observation.rationale_code,
            evidence_refs=observation.evidence_refs,
            backend_receipt_ref=observation.backend_receipt_ref,
            reconciled_at=utc_now(),
        )
        updated = replace(
            current,
            effect_status=observation.effect_status,
            reconciliation_status=next_reconciliation,
            receipt_refs=current.receipt_refs + (receipt.receipt_id,),
        )
        validate_operation_semantics(updated)
        return self._commit_projection(
            previous=current,
            updated=updated,
            event_type="operation.reconciled",
            receipt=receipt,
            receipt_id=receipt.receipt_id,
            receipt_type="reconciliation",
        )

    def pending_reconciliation(self) -> tuple[Operation, ...]:
        rows = self._host_store._db.execute(
            "SELECT operation_id FROM operation_projections ORDER BY operation_id"
        ).fetchall()
        pending: list[Operation] = []
        for row in rows:
            operation = self.get(row["operation_id"])
            if (
                operation.effect_status is EffectStatus.INDETERMINATE
                and operation.reconciliation_status
                in {ReconciliationStatus.PENDING, ReconciliationStatus.UNRESOLVED}
            ):
                pending.append(operation)
        return tuple(pending)

    def replay_is_intrinsically_safe(self, operation_id: str) -> bool:
        return retry_is_safe(self.get(operation_id))


class OperationHost:
    """Consumes K4 authority, dispatches once, and delegates truth to the journal."""

    def __init__(self, journal: OperationJournal, authority: AuthorityEngine):
        self._journal = journal
        self._authority = authority

    def execute_authorized(
        self,
        *,
        resolution: CapabilityResolution,
        authority_receipt_id: str,
        executor: EffectExecutor,
        idempotency_key: str | None = None,
    ) -> Operation:
        if idempotency_key is not None and not bool(
            getattr(executor, "supports_idempotency", False)
        ):
            raise InvalidRequest("execution backend does not support idempotency keys")

        authority_receipt = self._authority._store.get_execution_authority(
            authority_receipt_id
        )
        decision_context = self._authority._store.get_decision(
            authority_receipt.decision_id
        )
        if decision_context.resolution != resolution:
            raise IntegrityViolation(
                "execution resolution differs from authorized decision resolution"
            )
        operation = self._journal.create_intent(
            program_id=authority_receipt.program_id,
            actor_id=authority_receipt.actor_id,
            resolution=resolution,
            authority_receipt_ref=authority_receipt_id,
            idempotency_key=idempotency_key,
        )
        try:
            self._authority.consume_execution_authority(
                receipt_id=authority_receipt_id
            )
        except Exception:
            self._journal.fail_before_dispatch(
                operation.operation_id,
                error_code="authority_admission_failed",
            )
            raise

        self._journal.mark_admitted(operation.operation_id)
        running = self._journal.mark_running(operation.operation_id)
        effect = resolution.resolved_effect
        try:
            observation = executor.execute(
                effect,
                idempotency_key=idempotency_key,
            )
        except ExecutionTimeout:
            observation = self._exception_observation(
                effect, ExecutionOutcome.TIMED_OUT, "execution_timeout"
            )
        except ExecutionCancelled:
            observation = self._exception_observation(
                effect, ExecutionOutcome.CANCELLED, "execution_cancelled"
            )
        except ExecutionFailure:
            observation = self._exception_observation(
                effect, ExecutionOutcome.FAILED, "execution_failure"
            )
        except Exception:
            observation = self._exception_observation(
                effect, ExecutionOutcome.FAILED, "execution_backend_fault"
            )
        if not isinstance(observation, ExecutionObservation):
            observation = self._exception_observation(
                effect,
                ExecutionOutcome.FAILED,
                "invalid_execution_observation",
            )
        return self._journal.finish(running.operation_id, observation)

    @staticmethod
    def _exception_observation(
        effect: ResolvedEffect,
        outcome: ExecutionOutcome,
        error_code: str,
    ) -> ExecutionObservation:
        effect_status = (
            EffectStatus.NOT_APPLICABLE
            if effect.effect_class is EffectClass.OBSERVE
            else EffectStatus.INDETERMINATE
        )
        return ExecutionObservation(
            execution_outcome=outcome,
            effect_status=effect_status,
            output={},
            error_code=error_code,
        )

    def reconcile(
        self,
        operation_id: str,
        reconciler: EffectReconciler,
    ) -> Operation:
        operation = self._journal.get(operation_id)
        if operation.effect_status is not EffectStatus.INDETERMINATE:
            raise ReconciliationRequired("Operation does not require reconciliation")
        resolution = self._journal.resolution(operation_id)
        execution_receipt = self._journal.execution_receipt(operation_id)
        observation = reconciler.reconcile(
            resolution.resolved_effect,
            execution_receipt=execution_receipt,
            idempotency_key=self._journal.idempotency_key(operation_id),
        )
        if not isinstance(observation, ReconciliationObservation):
            raise IntegrityViolation("reconciler returned an invalid observation")
        return self._journal.apply_reconciliation(operation_id, observation)
