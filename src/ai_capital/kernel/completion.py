from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from uuid import uuid4

from .durable_program import ProgramRepository
from .enums import (
    CompletionResult,
    EffectClass,
    EffectStatus,
    ExecutionOutcome,
    ProgramStatus,
    VerificationResult,
    WorkItemStatus,
)
from .errors import (
    CompletionBlocked,
    IntegrityViolation,
    InvalidRequest,
    PersistenceConflict,
    StaleProgramRevision,
    VerificationStale,
)
from .events import event_digest_fields, make_program_event, utc_now, verify_event_digest
from .models import CapabilityResolution, CompletionReceipt, Event, Operation, Program
from .operation_journal import OperationJournal
from .program_state import transition_program
from .schema_codec import record_from_json, record_to_json
from .serialization import canonical_digest, canonical_json, to_canonical_data
from .verification import VerificationRepository


_COMPONENT = "completion"
_COMPONENT_SCHEMA_VERSION = 2


@dataclass(frozen=True, slots=True)
class CompletionBlocker:
    blocker_id: str
    program_id: str
    code: str
    detail: str
    opened_at: str


@dataclass(frozen=True, slots=True)
class CompletionBlockerResolution:
    blocker_id: str
    rationale_code: str
    resolved_at: str


class CompletionOracle:
    """Host-owned independent Program completion certification."""

    def __init__(
        self,
        host_store: ProgramRepository,
        verifications: VerificationRepository,
        operations: OperationJournal,
    ):
        if verifications._host_store is not host_store or operations._host_store is not host_store:
            raise InvalidRequest("Completion components must share one Host store")
        self._host_store = host_store
        self._verifications = verifications
        self._operations = operations
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
                    f"Completion schema version {version} is newer than supported "
                    f"{_COMPONENT_SCHEMA_VERSION}"
                )
            if version not in {None, 1, _COMPONENT_SCHEMA_VERSION}:
                raise IntegrityViolation(f"unsupported Completion schema version {version}")

            self._host_store._db.execute(
                """
                CREATE TABLE IF NOT EXISTS completion_receipts (
                    receipt_id TEXT PRIMARY KEY,
                    program_id TEXT NOT NULL,
                    program_revision INTEGER NOT NULL,
                    decision_event_id TEXT NOT NULL UNIQUE,
                    receipt_json TEXT NOT NULL,
                    receipt_digest TEXT NOT NULL
                )
                """
            )
            self._host_store._db.execute(
                """
                CREATE INDEX IF NOT EXISTS completion_receipts_program_revision
                ON completion_receipts(program_id, program_revision)
                """
            )
            self._host_store._db.execute(
                """
                CREATE TABLE IF NOT EXISTS completion_decision_event_index (
                    sequence INTEGER PRIMARY KEY,
                    program_id TEXT NOT NULL,
                    receipt_id TEXT NOT NULL UNIQUE,
                    event_id TEXT NOT NULL UNIQUE,
                    result TEXT NOT NULL,
                    FOREIGN KEY(sequence) REFERENCES events(sequence)
                )
                """
            )
            self._host_store._db.execute(
                """
                CREATE INDEX IF NOT EXISTS completion_decision_event_program_sequence
                ON completion_decision_event_index(program_id, sequence)
                """
            )
            self._host_store._db.execute(
                """
                CREATE INDEX IF NOT EXISTS events_type_sequence
                ON events(event_type, sequence)
                """
            )

            if version in {None, 1}:
                self._rebuild_decision_index()
                if version is None:
                    self._host_store._db.execute(
                        "INSERT INTO component_schema(component, version) VALUES (?, ?)",
                        (_COMPONENT, _COMPONENT_SCHEMA_VERSION),
                    )
                else:
                    self._host_store._db.execute(
                        "UPDATE component_schema SET version = ? WHERE component = ?",
                        (_COMPONENT_SCHEMA_VERSION, _COMPONENT),
                    )
                version = _COMPONENT_SCHEMA_VERSION

            if version != _COMPONENT_SCHEMA_VERSION:
                raise IntegrityViolation(f"unsupported Completion schema version {version}")
            self._validate_decision_index_alignment()

    def _append_event(
        self,
        event_type: str,
        payload: object,
        *,
        program_id: str,
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
            actor_id=None,
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
            correlation_id=program_id,
        )
        self._host_store._insert_event(event)
        return event

    def _event_by_id(self, event_id: str) -> Event:
        row = self._host_store._db.execute(
            """
            SELECT sequence, event_id, program_id, event_type, event_json, event_digest
            FROM events WHERE event_id = ?
            """,
            (event_id,),
        ).fetchone()
        if row is None:
            raise IntegrityViolation("Completion Event is missing")
        return self._decode_event_row(row)

    def _decode_event_row(self, row: sqlite3.Row) -> Event:
        try:
            event = record_from_json(Event, row["event_json"])
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("Completion Event cannot be decoded") from exc
        if not isinstance(event, Event):
            raise IntegrityViolation("Completion Event decoded wrong type")
        if (
            event.sequence != int(row["sequence"])
            or event.event_id != row["event_id"]
            or event.program_id != row["program_id"]
            or event.event_type != row["event_type"]
            or event.digest != row["event_digest"]
            or not verify_event_digest(event)
        ):
            raise IntegrityViolation("Completion Event integrity mismatch")
        return event

    def _decode_completion_decision_event(self, event: Event) -> CompletionReceipt:
        try:
            receipt = record_from_json(
                CompletionReceipt,
                canonical_json(event.payload["completion"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise IntegrityViolation("Completion decision Event is malformed") from exc
        if not isinstance(receipt, CompletionReceipt):
            raise IntegrityViolation("Completion decision Event decoded wrong type")
        expected_type = (
            "completion.certified"
            if receipt.result is CompletionResult.CERTIFIED
            else "completion.rejected"
        )
        if event.event_type != expected_type or event.correlation_id != receipt.program_id:
            raise IntegrityViolation("Completion decision Event binding mismatch")
        return receipt

    def _semantic_decision_event_set(self) -> set[tuple[str, str, str]]:
        rows = self._host_store._db.execute(
            """
            SELECT sequence, event_id, program_id, event_type, event_json, event_digest
            FROM events
            WHERE event_type IN ('completion.certified', 'completion.rejected')
            ORDER BY sequence
            """
        ).fetchall()
        semantic: set[tuple[str, str, str]] = set()
        receipt_ids: set[str] = set()
        for row in rows:
            event = self._decode_event_row(row)
            receipt = self._decode_completion_decision_event(event)
            if receipt.receipt_id in receipt_ids:
                raise IntegrityViolation("duplicate CompletionReceipt semantic identity")
            receipt_ids.add(receipt.receipt_id)
            semantic.add((receipt.receipt_id, receipt.program_id, event.event_id))
        return semantic

    def _rebuild_decision_index(self) -> None:
        self._host_store._db.execute("DELETE FROM completion_decision_event_index")
        rows = self._host_store._db.execute(
            """
            SELECT sequence, event_id, program_id, event_type, event_json, event_digest
            FROM events
            WHERE event_type IN ('completion.certified', 'completion.rejected')
            ORDER BY sequence
            """
        ).fetchall()
        for row in rows:
            event = self._decode_event_row(row)
            receipt = self._decode_completion_decision_event(event)
            self._host_store._db.execute(
                """
                INSERT INTO completion_decision_event_index(
                    sequence, program_id, receipt_id, event_id, result
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event.sequence,
                    receipt.program_id,
                    receipt.receipt_id,
                    event.event_id,
                    receipt.result.value,
                ),
            )

    def _validate_decision_index_alignment(self) -> None:
        receipt_rows = self._host_store._db.execute(
            "SELECT receipt_id, program_id, decision_event_id FROM completion_receipts"
        ).fetchall()
        index_rows = self._host_store._db.execute(
            "SELECT receipt_id, program_id, event_id FROM completion_decision_event_index"
        ).fetchall()
        receipts = {
            (str(row["receipt_id"]), str(row["program_id"]), str(row["decision_event_id"]))
            for row in receipt_rows
        }
        indexed = {
            (str(row["receipt_id"]), str(row["program_id"]), str(row["event_id"]))
            for row in index_rows
        }
        semantic = self._semantic_decision_event_set()
        if not (receipts == indexed == semantic):
            raise IntegrityViolation(
                "Completion receipt records/index diverge from semantic decision Events"
            )

    def open_blocker(self, program_id: str, *, code: str, detail: str) -> CompletionBlocker:
        program = self._host_store.get(program_id)
        if program.status in {
            ProgramStatus.COMPLETED,
            ProgramStatus.FAILED,
            ProgramStatus.CANCELLED,
        }:
            raise InvalidRequest("terminal Program cannot receive a completion blocker")
        if not code.strip() or not detail.strip():
            raise InvalidRequest("completion blocker code and detail must be non-empty")
        blocker = CompletionBlocker(
            blocker_id=str(uuid4()),
            program_id=program_id,
            code=code,
            detail=detail,
            opened_at=utc_now(),
        )
        with self._host_store._transaction():
            current = self._host_store.get(program_id)
            if current.status in {
                ProgramStatus.COMPLETED,
                ProgramStatus.FAILED,
                ProgramStatus.CANCELLED,
            }:
                raise InvalidRequest("terminal Program cannot receive a completion blocker")
            self._append_event(
                "completion.blocker_opened",
                {"blocker": blocker},
                program_id=program_id,
            )
        return blocker

    def _blocker_events(self) -> tuple[Event, ...]:
        rows = self._host_store._db.execute(
            """
            SELECT sequence, event_id, program_id, event_type, event_json, event_digest
            FROM events
            WHERE event_type IN ('completion.blocker_opened', 'completion.blocker_resolved')
            ORDER BY sequence
            """
        ).fetchall()
        return tuple(self._decode_event_row(row) for row in rows)

    def _blocker_state(self) -> dict[str, tuple[CompletionBlocker, bool]]:
        state: dict[str, tuple[CompletionBlocker, bool]] = {}
        for event in self._blocker_events():
            if event.event_type == "completion.blocker_opened":
                try:
                    blocker = record_from_json(
                        CompletionBlocker,
                        canonical_json(event.payload["blocker"]),
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise IntegrityViolation("completion blocker Event is malformed") from exc
                if not isinstance(blocker, CompletionBlocker):
                    raise IntegrityViolation("completion blocker Event decoded wrong type")
                if blocker.blocker_id in state or event.correlation_id != blocker.program_id:
                    raise IntegrityViolation("completion blocker Event identity conflict")
                state[blocker.blocker_id] = (blocker, False)
                continue

            try:
                resolution = record_from_json(
                    CompletionBlockerResolution,
                    canonical_json(event.payload["resolution"]),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise IntegrityViolation("completion blocker resolution Event is malformed") from exc
            if not isinstance(resolution, CompletionBlockerResolution):
                raise IntegrityViolation("completion blocker resolution decoded wrong type")
            existing = state.get(resolution.blocker_id)
            if existing is None or existing[1]:
                raise IntegrityViolation("completion blocker resolution has no open blocker")
            blocker = existing[0]
            if event.correlation_id != blocker.program_id or not resolution.rationale_code.strip():
                raise IntegrityViolation("completion blocker resolution binding mismatch")
            state[resolution.blocker_id] = (blocker, True)
        return state

    def unresolved_blockers(self, program_id: str) -> tuple[CompletionBlocker, ...]:
        blockers = [
            blocker
            for blocker, resolved in self._blocker_state().values()
            if blocker.program_id == program_id and not resolved
        ]
        return tuple(sorted(blockers, key=lambda blocker: blocker.blocker_id))

    def resolve_blocker(
        self,
        blocker_id: str,
        *,
        rationale_code: str = "completion_blocker_resolved",
    ) -> CompletionBlockerResolution:
        if not rationale_code.strip():
            raise InvalidRequest("completion blocker resolution requires a rationale code")
        existing = self._blocker_state().get(blocker_id)
        if existing is None:
            raise InvalidRequest(f"unknown completion blocker: {blocker_id}")
        blocker, resolved = existing
        if resolved:
            raise InvalidRequest("completion blocker is already resolved")
        resolution = CompletionBlockerResolution(
            blocker_id=blocker_id,
            rationale_code=rationale_code,
            resolved_at=utc_now(),
        )
        with self._host_store._transaction():
            current = self._blocker_state().get(blocker_id)
            if current is None or current[1]:
                raise PersistenceConflict("completion blocker changed before resolution")
            self._append_event(
                "completion.blocker_resolved",
                {"resolution": resolution},
                program_id=blocker.program_id,
            )
        return resolution

    def enter_completion_pending(
        self,
        program_id: str,
        *,
        expected_revision: int,
    ) -> Program:
        return self._host_store.transition(
            program_id,
            ProgramStatus.COMPLETION_PENDING,
            expected_revision=expected_revision,
        )

    def _requested_operations(
        self,
        program_id: str,
    ) -> dict[str, tuple[Operation, CapabilityResolution]]:
        rows = self._host_store._db.execute(
            """
            SELECT sequence, event_id, program_id, event_type, event_json, event_digest
            FROM events WHERE event_type = 'operation.requested' ORDER BY sequence
            """
        ).fetchall()
        requested: dict[str, tuple[Operation, CapabilityResolution]] = {}
        for row in rows:
            event = self._decode_event_row(row)
            try:
                operation = record_from_json(
                    Operation,
                    canonical_json(event.payload["operation"]),
                )
                resolution = record_from_json(
                    CapabilityResolution,
                    canonical_json(event.payload["resolution"]),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise IntegrityViolation("requested Operation Event is malformed") from exc
            if not isinstance(operation, Operation) or not isinstance(
                resolution, CapabilityResolution
            ):
                raise IntegrityViolation("requested Operation Event decoded wrong type")
            if (
                event.correlation_id != operation.program_id
                or event.actor_id != operation.actor_id
                or operation.capability_id != resolution.capability_id
                or operation.request_digest != canonical_digest(resolution)
            ):
                raise IntegrityViolation("requested Operation Event binding mismatch")
            if operation.program_id != program_id:
                continue
            if operation.operation_id in requested:
                raise IntegrityViolation("duplicate requested Operation identity")
            requested[operation.operation_id] = (operation, resolution)
        return requested

    @staticmethod
    def _operation_identity_matches_requested(
        current: Operation,
        requested: Operation,
    ) -> bool:
        return (
            current.operation_id == requested.operation_id
            and current.program_id == requested.program_id
            and current.actor_id == requested.actor_id
            and current.capability_id == requested.capability_id
            and current.authority_receipt_ref == requested.authority_receipt_ref
            and current.request_digest == requested.request_digest
        )

    def _latest_operation_event_states(
        self,
        program_id: str,
        requested: dict[str, tuple[Operation, CapabilityResolution]],
    ) -> dict[str, Operation]:
        rows = self._host_store._db.execute(
            """
            SELECT sequence, event_id, program_id, event_type, event_json, event_digest
            FROM events
            WHERE event_type IN (
                'operation.requested', 'operation.admitted', 'operation.started',
                'operation.finished', 'operation.interrupted', 'operation.reconciled'
            )
            ORDER BY sequence
            """
        ).fetchall()
        latest: dict[str, Operation] = {}
        for row in rows:
            event = self._decode_event_row(row)
            if event.correlation_id != program_id:
                continue
            try:
                operation = record_from_json(
                    Operation,
                    canonical_json(event.payload["operation"]),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise IntegrityViolation("Operation lifecycle Event is malformed") from exc
            if not isinstance(operation, Operation):
                raise IntegrityViolation("Operation lifecycle Event decoded wrong type")
            root = requested.get(operation.operation_id)
            if root is None:
                raise IntegrityViolation("Operation lifecycle Event lacks requested root")
            requested_operation = root[0]
            if (
                event.actor_id != operation.actor_id
                or operation.program_id != program_id
                or not self._operation_identity_matches_requested(
                    operation,
                    requested_operation,
                )
            ):
                raise IntegrityViolation("Operation lifecycle Event identity mismatch")
            if event.event_type == "operation.requested" and operation != requested_operation:
                raise IntegrityViolation("requested Operation lifecycle snapshot mismatch")
            latest[operation.operation_id] = operation
        if set(latest) != set(requested):
            raise IntegrityViolation("Operation lifecycle Event history is incomplete")
        return latest

    def _program_operations(self, program_id: str) -> tuple[tuple[Operation, EffectClass], ...]:
        projection_rows = self._host_store._db.execute(
            """
            SELECT operation_id FROM operation_projections
            WHERE program_id = ? ORDER BY operation_id
            """,
            (program_id,),
        ).fetchall()
        projection_ids = tuple(str(row["operation_id"]) for row in projection_rows)
        requested = self._requested_operations(program_id)
        requested_ids = tuple(requested)
        if set(projection_ids) != set(requested_ids) or len(projection_ids) != len(requested_ids):
            raise IntegrityViolation(
                "Program Operation projections diverge from requested Operation Events"
            )
        latest_states = self._latest_operation_event_states(program_id, requested)
        operations: list[tuple[Operation, EffectClass]] = []
        for operation_id in sorted(requested_ids):
            requested_operation, requested_resolution = requested[operation_id]
            operation = self._operations.get(operation_id)
            projected_resolution = self._operations.resolution(operation_id)
            if operation.program_id != program_id:
                raise IntegrityViolation("Operation query returned wrong Program identity")
            if not self._operation_identity_matches_requested(
                operation,
                requested_operation,
            ):
                raise IntegrityViolation(
                    "Operation projection identity diverges from requested Event"
                )
            if (
                projected_resolution != requested_resolution
                or operation.request_digest != canonical_digest(requested_resolution)
            ):
                raise IntegrityViolation(
                    "Operation resolution diverges from requested semantic Event"
                )
            if operation != latest_states[operation_id]:
                raise IntegrityViolation(
                    "Operation projection lifecycle diverges from semantic Events"
                )
            operations.append(
                (operation, requested_resolution.resolved_effect.effect_class)
            )
        return tuple(operations)

    @staticmethod
    def _criterion_reason(criterion: str) -> str:
        return f"success_criterion_unverified:{canonical_digest(criterion)[:16]}"

    def _evaluate(
        self,
        program: Program,
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        reasons: list[str] = []
        verification_refs: list[str] = []

        for work_item in program.work_items:
            if work_item.status is not WorkItemStatus.SATISFIED:
                reasons.append(f"required_work_outstanding:{work_item.work_item_id}")

        contracts = self._verifications.contracts_for_program(program.program_id)
        mandatory = tuple(contract for contract in contracts if contract.mandatory)
        if not mandatory:
            reasons.append("mandatory_verification_missing")

        covered_criteria: set[str] = set()
        require_effect_certainty = True if not mandatory else any(
            contract.require_effect_certainty for contract in mandatory
        )
        for contract in mandatory:
            covered_criteria.update(contract.success_criteria)
            latest = self._verifications.latest_for_contract(contract.contract_id)
            if latest is None:
                reasons.append(f"verification_missing:{contract.contract_id}")
                continue
            verification_refs.append(latest.verification_id)
            try:
                current = self._verifications.current(latest.verification_id)
            except VerificationStale:
                reasons.append(f"verification_stale:{contract.contract_id}")
                continue
            if current.result is VerificationResult.FAIL:
                reasons.append(f"verification_failed:{contract.contract_id}")
            elif current.result is VerificationResult.INDETERMINATE:
                reasons.append(f"verification_indeterminate:{contract.contract_id}")
            elif current.result is not VerificationResult.PASS:
                raise IntegrityViolation("unknown Verification result")

        for criterion in program.success_criteria:
            if criterion not in covered_criteria:
                reasons.append(self._criterion_reason(criterion))

        for blocker in self.unresolved_blockers(program.program_id):
            reasons.append(f"completion_blocker:{blocker.code}:{blocker.blocker_id}")

        operation_refs: list[str] = []
        for operation, effect_class in self._program_operations(program.program_id):
            operation_refs.append(operation.operation_id)
            if effect_class is EffectClass.OBSERVE:
                continue
            if operation.execution_outcome in {
                ExecutionOutcome.NOT_STARTED,
                ExecutionOutcome.RUNNING,
            }:
                reasons.append(f"protected_operation_outstanding:{operation.operation_id}")
            if require_effect_certainty and operation.effect_status is EffectStatus.INDETERMINATE:
                reasons.append(f"protected_effect_indeterminate:{operation.operation_id}")

        return (
            tuple(sorted(set(reasons))),
            tuple(sorted(set(verification_refs))),
            tuple(sorted(set(operation_refs))),
        )

    def _transition_program_in_transaction(
        self,
        current: Program,
        *,
        target: ProgramStatus,
        event_type: str,
    ) -> Program:
        updated = transition_program(
            current,
            target,
            expected_revision=current.revision,
        )
        sequence = self._host_store._next_sequence()
        event = make_program_event(
            sequence=sequence,
            event_type=event_type,
            program=updated,
        )
        self._host_store._insert_event(event)
        self._host_store._fault("completion_after_program_event")
        cursor = self._host_store._db.execute(
            """
            UPDATE program_projections
            SET revision = ?, projection_json = ?, projection_digest = ?, last_sequence = ?
            WHERE program_id = ? AND revision = ?
            """,
            (
                updated.revision,
                record_to_json(updated),
                canonical_digest(updated),
                event.sequence,
                updated.program_id,
                current.revision,
            ),
        )
        if cursor.rowcount != 1:
            raise StaleProgramRevision(
                f"Program revision changed during completion decision: {current.program_id}"
            )
        self._host_store._fault("completion_after_projection_write")
        return updated

    def decide(
        self,
        program_id: str,
        *,
        expected_revision: int,
    ) -> CompletionReceipt:
        receipt: CompletionReceipt
        with self._host_store._transaction():
            self._validate_decision_index_alignment()
            self._host_store.verify_integrity(program_id)
            current = self._host_store.get(program_id)
            if current.revision != expected_revision:
                raise StaleProgramRevision(
                    f"expected revision {expected_revision}, current revision {current.revision}"
                )
            if current.status is not ProgramStatus.COMPLETION_PENDING:
                raise CompletionBlocked("Program must be completion_pending before certification")

            reasons, verification_refs, operation_refs = self._evaluate(current)
            result = CompletionResult.REJECTED if reasons else CompletionResult.CERTIFIED
            rationale_codes = reasons if reasons else ("completion_certified",)
            receipt = CompletionReceipt(
                receipt_id=str(uuid4()),
                program_id=current.program_id,
                program_revision=current.revision,
                verification_refs=verification_refs,
                operation_refs=operation_refs,
                result=result,
                rationale_codes=rationale_codes,
                certified_at=utc_now(),
            )
            decision_event = self._append_event(
                "completion.rejected" if reasons else "completion.certified",
                {"completion": receipt},
                program_id=current.program_id,
            )
            try:
                self._host_store._db.execute(
                    """
                    INSERT INTO completion_decision_event_index(
                        sequence, program_id, receipt_id, event_id, result
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        decision_event.sequence,
                        receipt.program_id,
                        receipt.receipt_id,
                        decision_event.event_id,
                        receipt.result.value,
                    ),
                )
                self._host_store._db.execute(
                    """
                    INSERT INTO completion_receipts(
                        receipt_id, program_id, program_revision,
                        decision_event_id, receipt_json, receipt_digest
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        receipt.receipt_id,
                        receipt.program_id,
                        receipt.program_revision,
                        decision_event.event_id,
                        record_to_json(receipt),
                        canonical_digest(receipt),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise PersistenceConflict("Completion receipt identity collision") from exc
            self._host_store._fault("completion_after_decision_receipt")

            self._transition_program_in_transaction(
                current,
                target=ProgramStatus.ACTIVE if reasons else ProgramStatus.COMPLETED,
                event_type="program.completion_rejected" if reasons else "program.completed",
            )
        self._host_store._fault("completion_after_commit")
        return receipt

    def receipt(self, receipt_id: str) -> CompletionReceipt:
        index = self._host_store._db.execute(
            """
            SELECT sequence, program_id, receipt_id, event_id, result
            FROM completion_decision_event_index WHERE receipt_id = ?
            """,
            (receipt_id,),
        ).fetchone()
        if index is None:
            raise InvalidRequest(f"unknown CompletionReceipt: {receipt_id}")
        row = self._host_store._db.execute(
            """
            SELECT receipt_id, program_id, program_revision,
                   decision_event_id, receipt_json, receipt_digest
            FROM completion_receipts WHERE receipt_id = ?
            """,
            (receipt_id,),
        ).fetchone()
        if row is None:
            raise IntegrityViolation("Completion decision Event index lacks canonical receipt")
        try:
            receipt = record_from_json(CompletionReceipt, row["receipt_json"])
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("Completion receipt cannot be decoded") from exc
        if not isinstance(receipt, CompletionReceipt):
            raise IntegrityViolation("Completion receipt decoded wrong type")
        if (
            receipt.receipt_id != row["receipt_id"]
            or receipt.program_id != row["program_id"]
            or receipt.program_revision != int(row["program_revision"])
            or canonical_digest(receipt) != row["receipt_digest"]
            or index["program_id"] != receipt.program_id
            or index["event_id"] != row["decision_event_id"]
            or index["result"] != receipt.result.value
        ):
            raise IntegrityViolation("Completion receipt row/index integrity mismatch")
        event = self._event_by_id(str(row["decision_event_id"]))
        expected_type = (
            "completion.certified"
            if receipt.result is CompletionResult.CERTIFIED
            else "completion.rejected"
        )
        if (
            event.sequence != int(index["sequence"])
            or event.event_type != expected_type
            or event.correlation_id != receipt.program_id
            or to_canonical_data(event.payload.get("completion")) != to_canonical_data(receipt)
        ):
            raise IntegrityViolation("Completion receipt diverges from semantic Event")
        return receipt

    def receipts_for_program(self, program_id: str) -> tuple[CompletionReceipt, ...]:
        self._validate_decision_index_alignment()
        rows = self._host_store._db.execute(
            """
            SELECT receipt_id FROM completion_decision_event_index
            WHERE program_id = ? ORDER BY sequence
            """,
            (program_id,),
        ).fetchall()
        return tuple(self.receipt(str(row["receipt_id"])) for row in rows)
