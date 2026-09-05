from __future__ import annotations

import base64
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from uuid import uuid4

from .capability_store import CapabilityRepository
from .durable_program import ProgramRepository
from .enums import ContextCompleteness, ContextPriority
from .errors import (
    ContextBudgetExceeded,
    ContextIncomplete,
    IntegrityViolation,
    InvalidRequest,
    PersistenceConflict,
    StaleProgramRevision,
)
from .events import event_digest_fields, utc_now, verify_event_digest
from .evidence_store import EvidenceAdmissionReceipt, EvidenceRepository
from .frozen_json import FrozenMap, freeze_json
from .models import CapabilitySnapshot, ContextReceipt, Event, Evidence
from .schema_codec import record_from_json, record_to_json
from .serialization import canonical_digest, canonical_json, to_canonical_data


_COMPONENT = "bounded_context"
_COMPONENT_SCHEMA_VERSION = 1
_EVENT_REF_PREFIX = "event:"
_EVIDENCE_REF_PREFIX = "evidence:"
_CAPABILITY_REF_PREFIX = "capability_snapshot:"
_CONTEXT_RECEIPT_PREFIX = "context-receipt:"
_CONTEXT_ROOT_KEYS = frozenset({"sources", "capability_snapshot"})
_SOURCE_ENTRY_KEYS = frozenset(
    {
        "source_ref",
        "priority",
        "source_digest",
        "currentness",
        "authority",
        "historical",
        "payload",
    }
)
_PERSISTABLE_PRIORITIES = frozenset(
    {
        ContextPriority.HOST_CONTROL,
        ContextPriority.RECENT_INTERACTION,
        ContextPriority.ADVISORY_MEMORY,
    }
)
_PRIORITY_ORDER = {
    ContextPriority.HOST_CONTROL: 0,
    ContextPriority.CURRENT_PROGRAM: 1,
    ContextPriority.CURRENT_EVIDENCE: 2,
    ContextPriority.RECENT_INTERACTION: 3,
    ContextPriority.RECALLED_HISTORY: 4,
    ContextPriority.ADVISORY_MEMORY: 5,
}
_PRIORITY_SEMANTICS = {
    ContextPriority.HOST_CONTROL: ("current", "host_control", False),
    ContextPriority.CURRENT_PROGRAM: ("current", "current_program", False),
    ContextPriority.CURRENT_EVIDENCE: ("current", "evidence_only", False),
    ContextPriority.RECENT_INTERACTION: ("historical", "proposal_history", True),
    ContextPriority.RECALLED_HISTORY: ("historical", "historical_advisory", True),
    ContextPriority.ADVISORY_MEMORY: ("advisory", "advisory", False),
}


@dataclass(frozen=True, slots=True)
class PersistedContextSource:
    program_id: str
    program_revision: int
    priority: ContextPriority
    payload: FrozenMap
    source_digest: str
    currentness: str
    authority: str
    persisted_at: str

    def __post_init__(self) -> None:
        frozen = freeze_json(self.payload)
        if not isinstance(frozen, FrozenMap):
            raise TypeError("persisted Context source payload must be an object")
        object.__setattr__(self, "payload", frozen)


@dataclass(frozen=True, slots=True)
class _PersistedSourcePreflight:
    source_ref: str
    program_id: str
    program_revision: int
    priority: ContextPriority
    source_digest: str
    payload_units: int


@dataclass(frozen=True, slots=True)
class ContextSource:
    source_ref: str
    priority: ContextPriority
    payload: FrozenMap
    source_digest: str
    currentness: str
    authority: str
    historical: bool

    def __post_init__(self) -> None:
        frozen = freeze_json(self.payload)
        if not isinstance(frozen, FrozenMap):
            raise TypeError("Context source payload must be an object")
        object.__setattr__(self, "payload", frozen)


@dataclass(frozen=True, slots=True)
class CompiledContext:
    receipt: ContextReceipt
    context: FrozenMap
    used_units: int

    def __post_init__(self) -> None:
        frozen = freeze_json(self.context)
        if not isinstance(frozen, FrozenMap):
            raise TypeError("compiled Context must be an object")
        object.__setattr__(self, "context", frozen)


@dataclass(frozen=True, slots=True)
class RecallResult:
    requested_refs: tuple[str, ...]
    included_refs: tuple[str, ...]
    excluded_refs: tuple[str, ...]
    items: tuple[ContextSource, ...]
    completeness: ContextCompleteness
    budget_units: int
    used_units: int


def event_ref(event_id: str) -> str:
    return f"{_EVENT_REF_PREFIX}{event_id}"


def evidence_ref(evidence_id: str) -> str:
    return f"{_EVIDENCE_REF_PREFIX}{evidence_id}"


def context_receipt_ref(context_receipt_id: str) -> str:
    return context_receipt_id


def _canonical_units(value: object) -> int:
    return len(canonical_json(value).encode("utf-8"))


def _source_entry(source: ContextSource) -> dict[str, object]:
    return {
        "source_ref": source.source_ref,
        "priority": source.priority.value,
        "source_digest": source.source_digest,
        "currentness": source.currentness,
        "authority": source.authority,
        "historical": source.historical,
        "payload": source.payload,
    }


def _make_source(
    *,
    source_ref: str,
    priority: ContextPriority,
    payload: Mapping[str, object] | FrozenMap,
) -> ContextSource:
    frozen = freeze_json(payload)
    if not isinstance(frozen, FrozenMap):
        raise TypeError("Context source payload must be an object")
    currentness, authority, historical = _PRIORITY_SEMANTICS[priority]
    return ContextSource(
        source_ref=source_ref,
        priority=priority,
        payload=frozen,
        source_digest=canonical_digest(frozen),
        currentness=currentness,
        authority=authority,
        historical=historical,
    )


class ContextRepository:
    """Durable exact Context sources, receipts, and bounded historical recall."""

    def __init__(
        self,
        host_store: ProgramRepository,
        evidence: EvidenceRepository | None = None,
    ):
        if evidence is not None and evidence._host_store is not host_store:
            raise InvalidRequest("Context Evidence repository must share the Host store")
        self._host_store = host_store
        self._evidence = evidence
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
                    f"Context schema version {version} is newer than supported "
                    f"{_COMPONENT_SCHEMA_VERSION}"
                )
            if version not in {None, _COMPONENT_SCHEMA_VERSION}:
                raise IntegrityViolation(f"unsupported Context schema version {version}")

            self._host_store._db.execute(
                """
                CREATE TABLE IF NOT EXISTS context_receipts (
                    context_receipt_id TEXT PRIMARY KEY,
                    program_id TEXT NOT NULL,
                    program_revision INTEGER NOT NULL,
                    compiled_event_id TEXT NOT NULL UNIQUE,
                    receipt_json TEXT NOT NULL,
                    receipt_digest TEXT NOT NULL,
                    context_json TEXT NOT NULL,
                    context_digest TEXT NOT NULL,
                    used_units INTEGER NOT NULL
                )
                """
            )
            self._host_store._db.execute(
                """
                CREATE INDEX IF NOT EXISTS context_receipts_program_revision
                ON context_receipts(program_id, program_revision)
                """
            )
            self._host_store._db.execute(
                """
                CREATE TABLE IF NOT EXISTS context_receipt_event_index (
                    sequence INTEGER PRIMARY KEY,
                    program_id TEXT NOT NULL,
                    context_receipt_id TEXT NOT NULL UNIQUE,
                    event_id TEXT NOT NULL UNIQUE,
                    FOREIGN KEY(sequence) REFERENCES events(sequence)
                )
                """
            )
            self._host_store._db.execute(
                """
                CREATE INDEX IF NOT EXISTS context_receipt_event_program_sequence
                ON context_receipt_event_index(program_id, sequence)
                """
            )

            self._host_store._db.execute(
                """
                CREATE TABLE IF NOT EXISTS context_recall_event_index (
                    sequence INTEGER PRIMARY KEY,
                    event_id TEXT NOT NULL UNIQUE,
                    program_id TEXT,
                    event_type TEXT NOT NULL,
                    event_digest TEXT NOT NULL,
                    FOREIGN KEY(sequence) REFERENCES events(sequence)
                )
                """
            )
            self._host_store._db.execute(
                """
                CREATE INDEX IF NOT EXISTS context_recall_event_program_sequence
                ON context_recall_event_index(program_id, sequence)
                """
            )
            self._host_store._db.execute(
                """
                CREATE TABLE IF NOT EXISTS context_persisted_source_index (
                    sequence INTEGER PRIMARY KEY,
                    event_id TEXT NOT NULL UNIQUE,
                    program_id TEXT NOT NULL,
                    program_revision INTEGER NOT NULL,
                    priority TEXT NOT NULL,
                    source_digest TEXT NOT NULL,
                    payload_units INTEGER NOT NULL,
                    event_digest TEXT NOT NULL,
                    FOREIGN KEY(sequence) REFERENCES events(sequence)
                )
                """
            )
            self._host_store._db.execute(
                """
                CREATE INDEX IF NOT EXISTS context_persisted_source_program_priority
                ON context_persisted_source_index(program_id, priority, event_id)
                """
            )

            self._host_store._db.execute(
                """
                CREATE TRIGGER IF NOT EXISTS context_recall_event_index_insert
                AFTER INSERT ON events
                BEGIN
                    INSERT INTO context_recall_event_index(
                        sequence, event_id, program_id, event_type, event_digest
                    ) VALUES (
                        NEW.sequence,
                        NEW.event_id,
                        COALESCE(
                            NEW.program_id,
                            json_extract(NEW.event_json, '$.correlation_id')
                        ),
                        NEW.event_type,
                        NEW.event_digest
                    );
                END
                """
            )
            self._host_store._db.execute("DELETE FROM context_recall_event_index")
            self._host_store._db.execute(
                """
                INSERT INTO context_recall_event_index(
                    sequence, event_id, program_id, event_type, event_digest
                )
                SELECT
                    sequence,
                    event_id,
                    COALESCE(program_id, json_extract(event_json, '$.correlation_id')),
                    event_type,
                    event_digest
                FROM events
                """
            )

            self._rebuild_persisted_source_projection()

            if version is None:
                self._host_store._db.execute(
                    "INSERT INTO component_schema(component, version) VALUES (?, ?)",
                    (_COMPONENT, _COMPONENT_SCHEMA_VERSION),
                )

            # Context receipts are rebuildable projections over semantic Events.
            # Rebuilding on process start makes projection loss recoverable while the
            # exact Event history remains the durable authority.
            self._rebuild_receipt_projection()
            self.audit_integrity()

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

    def _decode_event_row(self, row: sqlite3.Row) -> Event:
        try:
            event = record_from_json(Event, row["event_json"])
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("Context Event cannot be decoded") from exc
        if not isinstance(event, Event):
            raise IntegrityViolation("Context Event decoded wrong type")
        if (
            event.sequence != int(row["sequence"])
            or event.event_id != row["event_id"]
            or event.program_id != row["program_id"]
            or event.event_type != row["event_type"]
            or event.digest != row["event_digest"]
            or not verify_event_digest(event)
        ):
            raise IntegrityViolation("Context Event integrity mismatch")
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
            raise InvalidRequest(f"unknown durable Context address: {event_ref(event_id)}")
        return self._decode_event_row(row)

    @staticmethod
    def _validate_receipt(receipt: ContextReceipt) -> None:
        if not receipt.context_receipt_id.startswith(_CONTEXT_RECEIPT_PREFIX):
            raise IntegrityViolation("Host ContextReceipt identity uses an invalid namespace")
        if not receipt.program_id.strip():
            raise IntegrityViolation("ContextReceipt Program identity must be non-empty")
        if receipt.program_revision < 0:
            raise IntegrityViolation("ContextReceipt Program revision must be non-negative")
        if receipt.budget_units <= 0:
            raise IntegrityViolation("ContextReceipt budget must be positive")
        if len(set(receipt.included_refs)) != len(receipt.included_refs):
            raise IntegrityViolation("ContextReceipt includes duplicate source references")
        if len(set(receipt.excluded_refs)) != len(receipt.excluded_refs):
            raise IntegrityViolation("ContextReceipt excludes duplicate source references")
        if set(receipt.included_refs) & set(receipt.excluded_refs):
            raise IntegrityViolation("ContextReceipt source references overlap")
        if receipt.completeness is ContextCompleteness.COMPLETE and receipt.excluded_refs:
            raise IntegrityViolation("complete ContextReceipt cannot exclude requested sources")
        if receipt.completeness is ContextCompleteness.TRUNCATED and not receipt.excluded_refs:
            raise IntegrityViolation("truncated ContextReceipt must identify excluded sources")
        if not receipt.created_at.strip():
            raise IntegrityViolation("ContextReceipt creation time must be non-empty")

    @staticmethod
    def _validate_source_entry(entry: object) -> str:
        if not isinstance(entry, FrozenMap):
            raise IntegrityViolation("compiled Context source entry must be an object")
        if set(entry) != _SOURCE_ENTRY_KEYS:
            raise IntegrityViolation("compiled Context source entry schema mismatch")
        try:
            source_ref_value = entry["source_ref"]
            priority = ContextPriority(str(entry["priority"]))
            source_digest = entry["source_digest"]
            currentness = entry["currentness"]
            authority = entry["authority"]
            historical = entry["historical"]
            payload = entry["payload"]
        except (KeyError, TypeError, ValueError) as exc:
            raise IntegrityViolation("compiled Context source entry is malformed") from exc
        if type(source_ref_value) is not str or not source_ref_value.strip():
            raise IntegrityViolation("compiled Context source reference must be non-empty")
        if type(source_digest) is not str or not source_digest.strip():
            raise IntegrityViolation("compiled Context source digest must be non-empty")
        if type(currentness) is not str or type(authority) is not str:
            raise IntegrityViolation("compiled Context source classification is malformed")
        if type(historical) is not bool:
            raise IntegrityViolation("compiled Context historical marker must be boolean")
        if not isinstance(payload, FrozenMap):
            raise IntegrityViolation("compiled Context source payload must be an object")
        expected_currentness, expected_authority, expected_historical = _PRIORITY_SEMANTICS[
            priority
        ]
        if (
            currentness != expected_currentness
            or authority != expected_authority
            or historical is not expected_historical
            or source_digest != canonical_digest(payload)
        ):
            raise IntegrityViolation("compiled Context source classification/digest mismatch")
        return source_ref_value

    @classmethod
    def _context_refs(cls, context: FrozenMap) -> tuple[str, ...]:
        if not isinstance(context, FrozenMap):
            raise IntegrityViolation("compiled Context must be a canonical object")
        if not set(context).issubset(_CONTEXT_ROOT_KEYS) or "sources" not in context:
            raise IntegrityViolation("compiled Context root schema mismatch")
        sources = context["sources"]
        if type(sources) is not tuple:
            raise IntegrityViolation("compiled Context sources must be an ordered array")
        source_refs: list[str] = []
        for entry in sources:
            ref = cls._validate_source_entry(entry)
            if ref in source_refs:
                raise IntegrityViolation("compiled Context contains duplicate sources")
            source_refs.append(ref)

        refs: list[str] = []
        if "capability_snapshot" in context:
            snapshot = context["capability_snapshot"]
            if not isinstance(snapshot, FrozenMap):
                raise IntegrityViolation("Capability snapshot Context payload must be an object")
            snapshot_id = snapshot.get("snapshot_id")
            if type(snapshot_id) is not str or not snapshot_id.strip():
                raise IntegrityViolation("Capability snapshot Context payload lacks identity")
            refs.append(f"{_CAPABILITY_REF_PREFIX}{snapshot_id}")
        refs.extend(source_refs)
        if len(set(refs)) != len(refs):
            raise IntegrityViolation("compiled Context source identities collide")
        return tuple(refs)

    def _decode_compiled_event(
        self,
        event: Event,
    ) -> tuple[ContextReceipt, FrozenMap, int]:
        if event.event_type != "context.compiled":
            raise IntegrityViolation("ContextReceipt is anchored to the wrong Event type")
        try:
            receipt = record_from_json(
                ContextReceipt,
                canonical_json(event.payload["receipt"]),
            )
            context = event.payload["context"]
            used_units = event.payload["used_units"]
        except (KeyError, TypeError, ValueError) as exc:
            raise IntegrityViolation("compiled Context Event is malformed") from exc
        if not isinstance(receipt, ContextReceipt) or not isinstance(context, FrozenMap):
            raise IntegrityViolation("compiled Context Event decoded wrong type")
        if type(used_units) is not int or used_units <= 0:
            raise IntegrityViolation("compiled Context Event has invalid size accounting")
        self._validate_receipt(receipt)
        refs = self._context_refs(context)
        if refs != receipt.included_refs:
            raise IntegrityViolation("ContextReceipt included sources differ from Context body")
        if event.correlation_id != receipt.program_id:
            raise IntegrityViolation("compiled Context Event Program binding mismatch")
        if used_units != _canonical_units(context):
            raise IntegrityViolation("compiled Context Event size accounting mismatch")
        if used_units > receipt.budget_units:
            raise IntegrityViolation("compiled Context exceeds its receipted budget")
        return receipt, context, used_units

    def _rebuild_persisted_source_projection(self) -> None:
        self._host_store._db.execute("DELETE FROM context_persisted_source_index")
        rows = self._host_store._db.execute(
            """
            SELECT sequence, event_id, program_id, event_type, event_json, event_digest
            FROM events WHERE event_type = 'context.source_persisted' ORDER BY sequence
            """
        ).fetchall()
        for row in rows:
            event = self._decode_event_row(row)
            if not event.correlation_id:
                raise IntegrityViolation("persisted Context source Event lacks Program binding")
            persisted, _ = self._source_from_persisted_event(
                event,
                expected_program_id=event.correlation_id,
            )
            try:
                self._host_store._db.execute(
                    """
                    INSERT INTO context_persisted_source_index(
                        sequence, event_id, program_id, program_revision, priority,
                        source_digest, payload_units, event_digest
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.sequence,
                        event.event_id,
                        persisted.program_id,
                        persisted.program_revision,
                        persisted.priority.value,
                        persisted.source_digest,
                        _canonical_units(persisted.payload),
                        event.digest,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise IntegrityViolation(
                    "persisted Context source projection rebuild collided"
                ) from exc

    def _semantic_receipts(self) -> dict[str, tuple[Event, ContextReceipt, FrozenMap, int]]:
        rows = self._host_store._db.execute(
            """
            SELECT sequence, event_id, program_id, event_type, event_json, event_digest
            FROM events WHERE event_type = 'context.compiled' ORDER BY sequence
            """
        ).fetchall()
        semantic: dict[str, tuple[Event, ContextReceipt, FrozenMap, int]] = {}
        for row in rows:
            event = self._decode_event_row(row)
            receipt, context, used_units = self._decode_compiled_event(event)
            if receipt.context_receipt_id in semantic:
                raise IntegrityViolation("duplicate ContextReceipt semantic identity")
            semantic[receipt.context_receipt_id] = (event, receipt, context, used_units)
        return semantic

    def _rebuild_receipt_projection(self) -> None:
        semantic = self._semantic_receipts()
        self._host_store._db.execute("DELETE FROM context_receipt_event_index")
        self._host_store._db.execute("DELETE FROM context_receipts")
        for event, receipt, context, used_units in semantic.values():
            try:
                self._host_store._db.execute(
                    """
                    INSERT INTO context_receipt_event_index(
                        sequence, program_id, context_receipt_id, event_id
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        event.sequence,
                        receipt.program_id,
                        receipt.context_receipt_id,
                        event.event_id,
                    ),
                )
                self._host_store._db.execute(
                    """
                    INSERT INTO context_receipts(
                        context_receipt_id, program_id, program_revision,
                        compiled_event_id, receipt_json, receipt_digest,
                        context_json, context_digest, used_units
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        receipt.context_receipt_id,
                        receipt.program_id,
                        receipt.program_revision,
                        event.event_id,
                        record_to_json(receipt),
                        canonical_digest(receipt),
                        canonical_json(context),
                        canonical_digest(context),
                        used_units,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise IntegrityViolation("Context receipt projection rebuild collided") from exc

    def _validate_receipt_alignment(self) -> None:
        semantic = self._semantic_receipts()
        receipt_rows = self._host_store._db.execute(
            """
            SELECT context_receipt_id, program_id, compiled_event_id
            FROM context_receipts
            """
        ).fetchall()
        index_rows = self._host_store._db.execute(
            """
            SELECT context_receipt_id, program_id, event_id
            FROM context_receipt_event_index
            """
        ).fetchall()
        receipts = {
            (
                str(row["context_receipt_id"]),
                str(row["program_id"]),
                str(row["compiled_event_id"]),
            )
            for row in receipt_rows
        }
        indexed = {
            (
                str(row["context_receipt_id"]),
                str(row["program_id"]),
                str(row["event_id"]),
            )
            for row in index_rows
        }
        semantic_ids = {
            (receipt_id, record[1].program_id, record[0].event_id)
            for receipt_id, record in semantic.items()
        }
        if not (receipts == indexed == semantic_ids):
            raise IntegrityViolation(
                "Context receipt records/index diverge from semantic Events"
            )
        for row in receipt_rows:
            self.get(str(row["context_receipt_id"]))

    def audit_integrity(self) -> None:
        """Run the full Host-wide Context receipt alignment audit explicitly."""
        self._validate_receipt_alignment()

    def persist_source(
        self,
        program_id: str,
        *,
        priority: ContextPriority,
        payload: Mapping[str, object],
    ) -> str:
        if priority not in _PERSISTABLE_PRIORITIES:
            raise InvalidRequest(
                "only Host control, recent interaction, or advisory memory may be "
                "persisted as a Context source"
            )
        program = self._host_store.get(program_id)
        frozen = freeze_json(payload)
        if not isinstance(frozen, FrozenMap):
            raise InvalidRequest("Context source payload must be an object")
        currentness, authority, _ = _PRIORITY_SEMANTICS[priority]
        source = PersistedContextSource(
            program_id=program.program_id,
            program_revision=program.revision,
            priority=priority,
            payload=frozen,
            source_digest=canonical_digest(frozen),
            currentness=currentness,
            authority=authority,
            persisted_at=utc_now(),
        )
        with self._host_store._transaction():
            current = self._host_store.get(program_id)
            if current.revision != program.revision:
                raise StaleProgramRevision(
                    "Program changed before Context source could be persisted"
                )
            event = self._append_event(
                "context.source_persisted",
                {"source": source},
                program_id=program_id,
            )
            try:
                self._host_store._db.execute(
                    """
                    INSERT INTO context_persisted_source_index(
                        sequence, event_id, program_id, program_revision, priority,
                        source_digest, payload_units, event_digest
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.sequence,
                        event.event_id,
                        source.program_id,
                        source.program_revision,
                        source.priority.value,
                        source.source_digest,
                        _canonical_units(source.payload),
                        event.digest,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise PersistenceConflict(
                    "persisted Context source metadata identity collision"
                ) from exc
        return event_ref(event.event_id)

    def _source_from_persisted_event(
        self,
        event: Event,
        *,
        expected_program_id: str,
    ) -> tuple[PersistedContextSource, ContextSource]:
        if event.event_type != "context.source_persisted":
            raise InvalidRequest("Context source reference does not identify persisted source")
        try:
            persisted = record_from_json(
                PersistedContextSource,
                canonical_json(event.payload["source"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise IntegrityViolation("persisted Context source Event is malformed") from exc
        if not isinstance(persisted, PersistedContextSource):
            raise IntegrityViolation("persisted Context source decoded wrong type")
        if (
            event.correlation_id != persisted.program_id
            or persisted.program_id != expected_program_id
            or persisted.priority not in _PERSISTABLE_PRIORITIES
            or persisted.source_digest != canonical_digest(persisted.payload)
        ):
            raise IntegrityViolation("persisted Context source binding/digest mismatch")
        currentness, authority, historical = _PRIORITY_SEMANTICS[persisted.priority]
        if persisted.currentness != currentness or persisted.authority != authority:
            raise IntegrityViolation("persisted Context source classification mismatch")
        source = ContextSource(
            source_ref=event_ref(event.event_id),
            priority=persisted.priority,
            payload=persisted.payload,
            source_digest=persisted.source_digest,
            currentness=currentness,
            authority=authority,
            historical=historical,
        )
        return persisted, source

    def persisted_source(self, program_id: str, source_ref: str) -> ContextSource:
        if not source_ref.startswith(_EVENT_REF_PREFIX):
            raise InvalidRequest("persisted Context source must use an Event address")
        event = self._event_by_id(source_ref[len(_EVENT_REF_PREFIX) :])
        _, source = self._source_from_persisted_event(
            event,
            expected_program_id=program_id,
        )
        return source

    def persisted_source_revision(self, program_id: str, source_ref: str) -> int:
        if not source_ref.startswith(_EVENT_REF_PREFIX):
            raise InvalidRequest("persisted Context source must use an Event address")
        event = self._event_by_id(source_ref[len(_EVENT_REF_PREFIX) :])
        persisted, _ = self._source_from_persisted_event(
            event,
            expected_program_id=program_id,
        )
        return persisted.program_revision

    def _persisted_source_preflight(
        self,
        program_id: str,
        source_ref: str,
    ) -> _PersistedSourcePreflight:
        if not source_ref.startswith(_EVENT_REF_PREFIX):
            raise InvalidRequest("persisted Context source must use an Event address")
        event_id = source_ref[len(_EVENT_REF_PREFIX) :]
        row = self._host_store._db.execute(
            """
            SELECT
                events.sequence AS event_sequence,
                events.event_type AS event_type,
                events.event_digest AS semantic_event_digest,
                context_persisted_source_index.sequence AS indexed_sequence,
                context_persisted_source_index.event_id AS indexed_event_id,
                context_persisted_source_index.program_id AS indexed_program_id,
                context_persisted_source_index.program_revision AS indexed_program_revision,
                context_persisted_source_index.priority AS indexed_priority,
                context_persisted_source_index.source_digest AS indexed_source_digest,
                context_persisted_source_index.payload_units AS indexed_payload_units,
                context_persisted_source_index.event_digest AS indexed_event_digest
            FROM events
            LEFT JOIN context_persisted_source_index
              ON context_persisted_source_index.event_id = events.event_id
            WHERE events.event_id = ?
            """,
            (event_id,),
        ).fetchone()
        if row is None:
            raise InvalidRequest(f"unknown durable Context address: {source_ref}")
        if row["indexed_event_id"] is None:
            if row["event_type"] == "context.source_persisted":
                raise IntegrityViolation(
                    "persisted Context source lacks durable metadata projection"
                )
            raise InvalidRequest(
                "Context source reference does not identify persisted source"
            )
        if (
            row["event_type"] != "context.source_persisted"
            or int(row["indexed_sequence"]) != int(row["event_sequence"])
            or row["indexed_event_digest"] != row["semantic_event_digest"]
        ):
            raise IntegrityViolation(
                "persisted Context source metadata diverges from Event row"
            )
        if row["indexed_program_id"] != program_id:
            raise InvalidRequest("persisted Context source belongs to a different Program")
        try:
            priority = ContextPriority(str(row["indexed_priority"]))
            program_revision = int(row["indexed_program_revision"])
            payload_units = int(row["indexed_payload_units"])
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("persisted Context source metadata is malformed") from exc
        source_digest = row["indexed_source_digest"]
        if (
            priority not in _PERSISTABLE_PRIORITIES
            or program_revision < 0
            or payload_units < _canonical_units({})
            or type(source_digest) is not str
            or not source_digest.strip()
        ):
            raise IntegrityViolation("persisted Context source metadata is invalid")
        return _PersistedSourcePreflight(
            source_ref=source_ref,
            program_id=program_id,
            program_revision=program_revision,
            priority=priority,
            source_digest=source_digest,
            payload_units=payload_units,
        )

    def _materialize_persisted_source(
        self,
        program_id: str,
        preflight: _PersistedSourcePreflight,
    ) -> ContextSource:
        event = self._event_by_id(preflight.source_ref[len(_EVENT_REF_PREFIX) :])
        persisted, source = self._source_from_persisted_event(
            event,
            expected_program_id=program_id,
        )
        if (
            persisted.program_id != preflight.program_id
            or persisted.program_revision != preflight.program_revision
            or persisted.priority is not preflight.priority
            or persisted.source_digest != preflight.source_digest
            or _canonical_units(persisted.payload) != preflight.payload_units
        ):
            raise IntegrityViolation(
                "persisted Context source materialization diverges from preflight"
            )
        return source

    def current_program_source(self, program_id: str) -> ContextSource:
        self._host_store.verify_integrity(program_id)
        program = self._host_store.get(program_id)
        row = self._host_store._db.execute(
            """
            SELECT last_sequence FROM program_projections WHERE program_id = ?
            """,
            (program_id,),
        ).fetchone()
        if row is None:
            raise InvalidRequest(f"unknown Program: {program_id}")
        event_row = self._host_store._db.execute(
            """
            SELECT sequence, event_id, program_id, event_type, event_json, event_digest
            FROM events WHERE sequence = ?
            """,
            (int(row["last_sequence"]),),
        ).fetchone()
        if event_row is None:
            raise IntegrityViolation("current Program projection lacks semantic Event")
        event = self._decode_event_row(event_row)
        if event.program_id != program_id:
            raise IntegrityViolation("current Program Event has wrong Program identity")
        try:
            payload = event.payload["program"]
        except KeyError as exc:
            raise IntegrityViolation("current Program Event lacks Program snapshot") from exc
        if not isinstance(payload, FrozenMap):
            raise IntegrityViolation("current Program Event snapshot is malformed")
        if canonical_json(payload) != canonical_json(to_canonical_data(program)):
            raise IntegrityViolation("current Program Event differs from Program projection")
        return _make_source(
            source_ref=event_ref(event.event_id),
            priority=ContextPriority.CURRENT_PROGRAM,
            payload={"program": payload},
        )

    def _event_belongs_to_program(self, event: Event, program_id: str) -> bool:
        return event.program_id == program_id or event.correlation_id == program_id

    def _historical_event_source(self, event: Event, program_id: str) -> ContextSource:
        if not self._event_belongs_to_program(event, program_id):
            raise InvalidRequest("historical Event belongs to a different Program")
        if event.event_type == "context.source_persisted":
            _, persisted = self._source_from_persisted_event(
                event,
                expected_program_id=program_id,
            )
            return _make_source(
                source_ref=persisted.source_ref,
                priority=ContextPriority.RECALLED_HISTORY,
                payload=persisted.payload,
            )
        return _make_source(
            source_ref=event_ref(event.event_id),
            priority=ContextPriority.RECALLED_HISTORY,
            payload={"event": to_canonical_data(event)},
        )

    def _historical_evidence_source(self, source_ref: str) -> ContextSource:
        if self._evidence is None:
            raise InvalidRequest("Evidence recall requires the Host Evidence repository")
        evidence_id = source_ref[len(_EVIDENCE_REF_PREFIX) :]
        evidence = self._evidence.get(evidence_id)
        artifact = self._evidence.artifact(evidence_id)
        return _make_source(
            source_ref=source_ref,
            priority=ContextPriority.RECALLED_HISTORY,
            payload={
                "evidence": to_canonical_data(evidence),
                "content_base64": base64.b64encode(artifact).decode("ascii"),
            },
        )

    def _historical_context_source(self, source_ref: str) -> ContextSource:
        compiled = self.get(source_ref)
        return _make_source(
            source_ref=source_ref,
            priority=ContextPriority.RECALLED_HISTORY,
            payload={
                "receipt": to_canonical_data(compiled.receipt),
                "context": compiled.context,
                "used_units": compiled.used_units,
            },
        )

    def _validate_recall_address(self, program_id: str, source_ref: str) -> None:

        if source_ref.startswith(_EVENT_REF_PREFIX):
            event_id = source_ref[len(_EVENT_REF_PREFIX) :]
            row = self._host_store._db.execute(
                """
                SELECT
                    events.sequence,
                    events.program_id AS event_program_id,
                    events.event_type,
                    events.event_digest,
                    context_recall_event_index.sequence AS indexed_sequence,
                    context_recall_event_index.program_id AS indexed_program_id,
                    context_recall_event_index.event_type AS indexed_event_type,
                    context_recall_event_index.event_digest AS indexed_event_digest
                FROM events
                LEFT JOIN context_recall_event_index
                  ON context_recall_event_index.event_id = events.event_id
                WHERE events.event_id = ?
                """,
                (event_id,),
            ).fetchone()
            if row is None:
                raise InvalidRequest(
                    f"unknown durable Context address: {event_ref(event_id)}"
                )
            if row["indexed_sequence"] is None:
                raise IntegrityViolation("Event recall address lacks durable metadata index")
            if (
                int(row["indexed_sequence"]) != int(row["sequence"])
                or row["indexed_event_type"] != row["event_type"]
                or row["indexed_event_digest"] != row["event_digest"]
                or (
                    row["event_program_id"] is not None
                    and row["indexed_program_id"] != row["event_program_id"]
                )
            ):
                raise IntegrityViolation("Event recall metadata index diverges from Event row")
            if row["indexed_program_id"] != program_id:
                raise InvalidRequest("historical Event belongs to a different Program")
            return
        if source_ref.startswith(_EVIDENCE_REF_PREFIX):
            if self._evidence is None:
                raise InvalidRequest("Evidence recall requires the Host Evidence repository")
            evidence_id = source_ref[len(_EVIDENCE_REF_PREFIX) :]
            self._evidence._row(evidence_id)
            return
        if source_ref.startswith(_CONTEXT_RECEIPT_PREFIX):
            row = self._host_store._db.execute(
                """
                SELECT program_id, compiled_event_id
                FROM context_receipts WHERE context_receipt_id = ?
                """,
                (source_ref,),
            ).fetchone()
            if row is None:
                raise InvalidRequest(f"unknown ContextReceipt: {source_ref}")
            index = self._host_store._db.execute(
                """
                SELECT program_id, event_id
                FROM context_receipt_event_index WHERE context_receipt_id = ?
                """,
                (source_ref,),
            ).fetchone()
            if (
                index is None
                or index["program_id"] != row["program_id"]
                or index["event_id"] != row["compiled_event_id"]
            ):
                raise IntegrityViolation("ContextReceipt lacks valid semantic Event binding")
            if row["program_id"] != program_id:
                raise InvalidRequest("historical Context belongs to a different Program")
            return
        raise InvalidRequest(f"unsupported durable recall address: {source_ref}")

    def _resolve_recall(self, program_id: str, source_ref: str) -> ContextSource:
        if source_ref.startswith(_EVENT_REF_PREFIX):
            event = self._event_by_id(source_ref[len(_EVENT_REF_PREFIX) :])
            return self._historical_event_source(event, program_id)
        if source_ref.startswith(_EVIDENCE_REF_PREFIX):
            return self._historical_evidence_source(source_ref)
        if source_ref.startswith(_CONTEXT_RECEIPT_PREFIX):
            source = self._historical_context_source(source_ref)
            compiled = self.get(source_ref)
            if compiled.receipt.program_id != program_id:
                raise InvalidRequest("historical Context belongs to a different Program")
            return source
        raise InvalidRequest(f"unsupported durable recall address: {source_ref}")

    def recall(
        self,
        program_id: str,
        source_refs: tuple[str, ...],
        *,
        max_items: int,
        max_units: int,
    ) -> RecallResult:
        self._host_store.get(program_id)
        if max_items <= 0 or max_units <= 0:
            raise InvalidRequest("bounded recall requires positive item and size limits")
        if len(set(source_refs)) != len(source_refs):
            raise InvalidRequest("bounded recall does not accept duplicate source addresses")

        minimum_units = _canonical_units({"sources": tuple()})
        if max_units < minimum_units:
            raise ContextBudgetExceeded(
                "bounded recall budget cannot fit the empty Context envelope"
            )

        ordered_refs = tuple(sorted(source_refs))
        for source_ref_value in ordered_refs:
            self._validate_recall_address(program_id, source_ref_value)

        items: list[ContextSource] = []
        included: list[str] = []
        excluded: list[str] = []
        materialization_attempts = 0
        for source_ref_value in ordered_refs:
            if materialization_attempts >= max_items:
                excluded.append(source_ref_value)
                continue
            materialization_attempts += 1
            source = self._resolve_recall(program_id, source_ref_value)
            trial = {
                "sources": tuple(_source_entry(item) for item in (*items, source))
            }
            if _canonical_units(trial) > max_units:
                excluded.append(source_ref_value)
                continue
            items.append(source)
            included.append(source_ref_value)

        used_units = _canonical_units(
            {"sources": tuple(_source_entry(item) for item in items)}
        )
        completeness = (
            ContextCompleteness.TRUNCATED if excluded else ContextCompleteness.COMPLETE
        )
        return RecallResult(
            requested_refs=ordered_refs,
            included_refs=tuple(included),
            excluded_refs=tuple(excluded),
            items=tuple(items),
            completeness=completeness,
            budget_units=max_units,
            used_units=used_units,
        )

    def _store_compilation(
        self,
        receipt: ContextReceipt,
        context: FrozenMap,
        used_units: int,
    ) -> CompiledContext:
        self._validate_receipt(receipt)
        refs = self._context_refs(context)
        if refs != receipt.included_refs:
            raise IntegrityViolation("ContextReceipt does not describe compiled Context sources")
        if used_units != _canonical_units(context) or used_units > receipt.budget_units:
            raise ContextBudgetExceeded("compiled Context exceeds its receipted budget")

        with self._host_store._transaction():
            current = self._host_store.get(receipt.program_id)
            if current.revision != receipt.program_revision:
                raise StaleProgramRevision(
                    "Program changed before compiled Context could be receipted"
                )
            event = self._append_event(
                "context.compiled",
                {
                    "receipt": receipt,
                    "context": context,
                    "used_units": used_units,
                },
                program_id=receipt.program_id,
            )
            try:
                self._host_store._db.execute(
                    """
                    INSERT INTO context_receipt_event_index(
                        sequence, program_id, context_receipt_id, event_id
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        event.sequence,
                        receipt.program_id,
                        receipt.context_receipt_id,
                        event.event_id,
                    ),
                )
                self._host_store._db.execute(
                    """
                    INSERT INTO context_receipts(
                        context_receipt_id, program_id, program_revision,
                        compiled_event_id, receipt_json, receipt_digest,
                        context_json, context_digest, used_units
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        receipt.context_receipt_id,
                        receipt.program_id,
                        receipt.program_revision,
                        event.event_id,
                        record_to_json(receipt),
                        canonical_digest(receipt),
                        canonical_json(context),
                        canonical_digest(context),
                        used_units,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise PersistenceConflict("ContextReceipt identity collision") from exc
        return CompiledContext(receipt=receipt, context=context, used_units=used_units)

    def get(self, context_receipt_id: str) -> CompiledContext:
        row = self._host_store._db.execute(
            """
            SELECT context_receipt_id, program_id, program_revision,
                   compiled_event_id, receipt_json, receipt_digest,
                   context_json, context_digest, used_units
            FROM context_receipts WHERE context_receipt_id = ?
            """,
            (context_receipt_id,),
        ).fetchone()
        if row is None:
            raise InvalidRequest(f"unknown ContextReceipt: {context_receipt_id}")
        index = self._host_store._db.execute(
            """
            SELECT sequence, program_id, context_receipt_id, event_id
            FROM context_receipt_event_index WHERE context_receipt_id = ?
            """,
            (context_receipt_id,),
        ).fetchone()
        if index is None:
            raise IntegrityViolation("ContextReceipt lacks semantic Event index")
        try:
            receipt = record_from_json(ContextReceipt, row["receipt_json"])
            decoded_context = freeze_json(json.loads(row["context_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise IntegrityViolation("ContextReceipt record cannot be decoded") from exc
        if not isinstance(receipt, ContextReceipt) or not isinstance(
            decoded_context, FrozenMap
        ):
            raise IntegrityViolation("ContextReceipt record decoded wrong type")
        used_units = int(row["used_units"])
        self._validate_receipt(receipt)
        refs = self._context_refs(decoded_context)
        if (
            receipt.context_receipt_id != row["context_receipt_id"]
            or receipt.program_id != row["program_id"]
            or receipt.program_revision != int(row["program_revision"])
            or canonical_digest(receipt) != row["receipt_digest"]
            or canonical_digest(decoded_context) != row["context_digest"]
            or refs != receipt.included_refs
            or used_units != _canonical_units(decoded_context)
            or used_units > receipt.budget_units
            or index["program_id"] != receipt.program_id
            or index["event_id"] != row["compiled_event_id"]
        ):
            raise IntegrityViolation("ContextReceipt row/index integrity mismatch")
        event = self._event_by_id(str(row["compiled_event_id"]))
        semantic_receipt, semantic_context, semantic_units = self._decode_compiled_event(event)
        if (
            event.sequence != int(index["sequence"])
            or semantic_receipt != receipt
            or semantic_context != decoded_context
            or semantic_units != used_units
        ):
            raise IntegrityViolation("ContextReceipt diverges from semantic Event")
        return CompiledContext(receipt=receipt, context=decoded_context, used_units=used_units)

    def validate(
        self,
        receipt: ContextReceipt,
        context: Mapping[str, object] | FrozenMap,
    ) -> CompiledContext:
        durable = self.get(receipt.context_receipt_id)
        frozen = freeze_json(context)
        if not isinstance(frozen, FrozenMap):
            raise IntegrityViolation("inference Context must be an object")
        if durable.receipt != receipt or durable.context != frozen:
            raise IntegrityViolation(
                "inference Context differs from durable ContextReceipt compilation"
            )
        return durable

    def receipts_for_program(self, program_id: str) -> tuple[ContextReceipt, ...]:
        rows = self._host_store._db.execute(
            """
            SELECT context_receipt_id FROM context_receipt_event_index
            WHERE program_id = ? ORDER BY sequence
            """,
            (program_id,),
        ).fetchall()
        return tuple(
            self.get(str(row["context_receipt_id"])).receipt for row in rows
        )


class ContextCompiler:
    """Deterministic bounded projection over durable AI Capital sources."""

    def __init__(
        self,
        contexts: ContextRepository,
        *,
        evidence: EvidenceRepository | None = None,
        capabilities: CapabilityRepository | None = None,
    ):
        if evidence is not None:
            if evidence._host_store is not contexts._host_store:
                raise InvalidRequest("Context Evidence repository must share the Host store")
            if contexts._evidence is not evidence:
                raise InvalidRequest(
                    "Context Compiler Evidence repository must match the Context repository"
                )
        if capabilities is not None and capabilities._host_store is not contexts._host_store:
            raise InvalidRequest("Context Capability repository must share the Host store")
        self._contexts = contexts
        self._host_store = contexts._host_store
        self._evidence = evidence
        self._capabilities = capabilities

    def _current_evidence_preflight(self, evidence_id: str) -> tuple[Evidence, int]:
        if self._evidence is None:
            raise InvalidRequest("current Evidence Context requires the Evidence repository")
        row = self._evidence._row(evidence_id)
        try:
            evidence = record_from_json(Evidence, row["evidence_json"])
            admission = record_from_json(
                EvidenceAdmissionReceipt,
                row["admission_json"],
            )
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("Evidence record cannot be preflighted") from exc
        if not isinstance(evidence, Evidence) or not isinstance(
            admission, EvidenceAdmissionReceipt
        ):
            raise IntegrityViolation("Evidence preflight decoded wrong type")
        if (
            evidence.evidence_id != row["evidence_id"]
            or evidence.digest != row["artifact_digest"]
            or canonical_digest(evidence) != row["evidence_record_digest"]
            or canonical_digest(admission) != row["admission_digest"]
            or admission.evidence_id != evidence.evidence_id
            or admission.artifact_digest != evidence.digest
        ):
            raise IntegrityViolation("Evidence preflight record binding mismatch")
        self._evidence._validate_evidence(evidence)
        if evidence.currentness != "current":
            raise ContextIncomplete(
                f"Evidence is not current and cannot enter current-evidence Context: {evidence_id}"
            )

        artifact = self._host_store._db.execute(
            """
            SELECT content_ref, byte_length
            FROM evidence_artifacts WHERE artifact_digest = ?
            """,
            (evidence.digest,),
        ).fetchone()
        if artifact is None:
            raise IntegrityViolation("Evidence artifact metadata is missing")
        byte_length = int(artifact["byte_length"])
        if byte_length <= 0 or artifact["content_ref"] != evidence.content_ref:
            raise IntegrityViolation("Evidence artifact metadata binding mismatch")
        artifact_path = self._evidence._artifact_path(evidence.digest)
        try:
            stored_length = artifact_path.stat().st_size
        except FileNotFoundError as exc:
            raise IntegrityViolation("Evidence artifact is missing") from exc
        except OSError as exc:
            raise IntegrityViolation("Evidence artifact metadata cannot be read") from exc
        if stored_length != byte_length:
            raise IntegrityViolation("Evidence artifact byte length mismatch")

        indexed = self._host_store._db.execute(
            """
            SELECT
                evidence_event_index.sequence AS indexed_sequence,
                evidence_event_index.evidence_id AS indexed_evidence_id,
                evidence_event_index.event_type AS indexed_event_type,
                events.sequence AS event_sequence,
                events.event_id AS semantic_event_id,
                events.event_type AS semantic_event_type
            FROM evidence_event_index
            JOIN events ON events.event_id = evidence_event_index.event_id
            WHERE evidence_event_index.event_id = ?
            """,
            (row["admitted_event_id"],),
        ).fetchone()
        if (
            indexed is None
            or int(indexed["indexed_sequence"]) != int(indexed["event_sequence"])
            or indexed["indexed_evidence_id"] != evidence.evidence_id
            or indexed["indexed_event_type"] != "evidence.admitted"
            or indexed["semantic_event_id"] != row["admitted_event_id"]
            or indexed["semantic_event_type"] != "evidence.admitted"
        ):
            raise IntegrityViolation("Evidence preflight Event binding mismatch")
        return evidence, byte_length

    def _current_evidence_source(self, evidence: Evidence) -> ContextSource:
        if self._evidence is None:
            raise InvalidRequest("current Evidence Context requires the Evidence repository")
        artifact = self._evidence.artifact(evidence.evidence_id)
        return _make_source(
            source_ref=evidence_ref(evidence.evidence_id),
            priority=ContextPriority.CURRENT_EVIDENCE,
            payload={
                "evidence": to_canonical_data(evidence),
                "content_base64": base64.b64encode(artifact).decode("ascii"),
            },
        )

    def _capability_context(
        self,
        capability_snapshot: CapabilitySnapshot | None,
    ) -> tuple[str | None, object | None]:
        if capability_snapshot is None:
            return None, None
        if self._capabilities is None:
            raise InvalidRequest(
                "Capability snapshot Context requires the Host Capability repository"
            )
        durable = self._capabilities.get_snapshot(capability_snapshot.snapshot_id)
        if durable != capability_snapshot:
            raise IntegrityViolation(
                "Capability snapshot differs from durable Host Context source"
            )
        return (
            f"{_CAPABILITY_REF_PREFIX}{durable.snapshot_id}",
            to_canonical_data(durable),
        )

    @staticmethod
    def _sort_sources(sources: list[ContextSource]) -> list[ContextSource]:
        return sorted(
            sources,
            key=lambda source: (_PRIORITY_ORDER[source.priority], source.source_ref),
        )

    @staticmethod
    def _build_context(
        sources: list[ContextSource],
        capability_payload: object | None,
    ) -> FrozenMap:
        payload: dict[str, object] = {
            "sources": tuple(_source_entry(source) for source in sources)
        }
        if capability_payload is not None:
            payload["capability_snapshot"] = capability_payload
        frozen = freeze_json(payload)
        assert isinstance(frozen, FrozenMap)
        return frozen

    def compile(
        self,
        program_id: str,
        *,
        budget_units: int,
        source_refs: tuple[str, ...] = (),
        evidence_refs: tuple[str, ...] = (),
        recalled_refs: tuple[str, ...] = (),
        recall_max_items: int = 8,
        recall_max_units: int | None = None,
        capability_snapshot: CapabilitySnapshot | None = None,
        coverage_complete: bool = True,
    ) -> CompiledContext:
        if budget_units <= 0:
            raise ContextBudgetExceeded("Context budget must be positive")
        if len(set(source_refs)) != len(source_refs):
            raise InvalidRequest("Context compilation contains duplicate durable sources")

        program = self._host_store.get(program_id)
        program_source = self._contexts.current_program_source(program_id)
        persisted_preflights = tuple(
            self._contexts._persisted_source_preflight(program_id, source_ref_value)
            for source_ref_value in source_refs
        )
        for preflight in persisted_preflights:
            if (
                preflight.priority is ContextPriority.HOST_CONTROL
                and preflight.program_revision != program.revision
            ):
                raise ContextIncomplete(
                    "Host control Context source is stale for current Program revision"
                )

        recall_result: RecallResult | None = None
        recalled_sources: list[ContextSource] = []
        if recalled_refs:
            recall_result = self._contexts.recall(
                program_id,
                recalled_refs,
                max_items=recall_max_items,
                max_units=budget_units if recall_max_units is None else recall_max_units,
            )
            recalled_sources = self._sort_sources(list(recall_result.items))

        evidence_source_refs = tuple(
            evidence_ref(evidence_id) for evidence_id in evidence_refs
        )
        source_ids = (
            [program_source.source_ref]
            + [preflight.source_ref for preflight in persisted_preflights]
            + [source.source_ref for source in recalled_sources]
            + list(evidence_source_refs)
        )
        if len(set(source_ids)) != len(source_ids):
            raise InvalidRequest("Context compilation contains duplicate durable sources")

        capability_ref_value, capability_payload = self._capability_context(
            capability_snapshot
        )
        if capability_ref_value is not None and capability_ref_value in source_ids:
            raise InvalidRequest("Capability snapshot Context identity collides with source")

        ordered_preflights = sorted(
            persisted_preflights,
            key=lambda item: (_PRIORITY_ORDER[item.priority], item.source_ref),
        )
        host_controls = tuple(
            item for item in ordered_preflights
            if item.priority is ContextPriority.HOST_CONTROL
        )
        recent_sources = tuple(
            item for item in ordered_preflights
            if item.priority is ContextPriority.RECENT_INTERACTION
        )
        advisory_sources = tuple(
            item for item in ordered_preflights
            if item.priority is ContextPriority.ADVISORY_MEMORY
        )

        included_sources: list[ContextSource] = []
        included_refs: list[str] = []
        excluded_refs: list[str] = []
        if capability_ref_value is not None:
            included_refs.append(capability_ref_value)

        def consider_persisted(
            preflight: _PersistedSourcePreflight,
            *,
            mandatory: bool,
        ) -> None:
            currentness, authority, historical = _PRIORITY_SEMANTICS[preflight.priority]
            empty_payload = freeze_json({})
            assert isinstance(empty_payload, FrozenMap)
            shell = ContextSource(
                source_ref=preflight.source_ref,
                priority=preflight.priority,
                payload=empty_payload,
                source_digest=preflight.source_digest,
                currentness=currentness,
                authority=authority,
                historical=historical,
            )
            shell_trial = self._build_context(
                [*included_sources, shell],
                capability_payload,
            )
            predicted_units = (
                _canonical_units(shell_trial)
                - _canonical_units(empty_payload)
                + preflight.payload_units
            )
            if predicted_units > budget_units:
                if mandatory:
                    raise ContextBudgetExceeded(
                        "Context budget cannot fit mandatory Host control/current Program sources"
                    )
                excluded_refs.append(preflight.source_ref)
                return

            source = self._contexts._materialize_persisted_source(
                program_id,
                preflight,
            )
            trial = self._build_context(
                [*included_sources, source],
                capability_payload,
            )
            actual_units = _canonical_units(trial)
            if actual_units != predicted_units:
                raise IntegrityViolation(
                    "persisted Context source size preflight diverges from materialization"
                )
            included_sources.append(source)
            included_refs.append(source.source_ref)

        for preflight in host_controls:
            consider_persisted(preflight, mandatory=True)

        program_trial = self._build_context(
            [*included_sources, program_source],
            capability_payload,
        )
        if _canonical_units(program_trial) > budget_units:
            raise ContextBudgetExceeded(
                "Context budget cannot fit mandatory Host control/current Program sources"
            )
        included_sources.append(program_source)
        included_refs.append(program_source.source_ref)

        for evidence_id in sorted(evidence_refs):
            evidence, byte_length = self._current_evidence_preflight(evidence_id)
            source_ref_value = evidence_ref(evidence_id)
            shell = _make_source(
                source_ref=source_ref_value,
                priority=ContextPriority.CURRENT_EVIDENCE,
                payload={
                    "evidence": to_canonical_data(evidence),
                    "content_base64": "",
                },
            )
            encoded_length = 4 * ((byte_length + 2) // 3)
            shell_trial = self._build_context(
                [*included_sources, shell],
                capability_payload,
            )
            predicted_units = _canonical_units(shell_trial) + encoded_length
            if predicted_units > budget_units:
                excluded_refs.append(source_ref_value)
                continue

            source = self._current_evidence_source(evidence)
            trial = self._build_context(
                [*included_sources, source],
                capability_payload,
            )
            actual_units = _canonical_units(trial)
            if actual_units != predicted_units:
                raise IntegrityViolation(
                    "Evidence Context size preflight diverges from materialized source"
                )
            included_sources.append(source)
            included_refs.append(source_ref_value)

        for preflight in recent_sources:
            consider_persisted(preflight, mandatory=False)

        for source in recalled_sources:
            trial = self._build_context(
                [*included_sources, source],
                capability_payload,
            )
            if _canonical_units(trial) > budget_units:
                excluded_refs.append(source.source_ref)
                continue
            included_sources.append(source)
            included_refs.append(source.source_ref)

        for preflight in advisory_sources:
            consider_persisted(preflight, mandatory=False)

        if recall_result is not None:
            for ref in recall_result.excluded_refs:
                if ref not in excluded_refs and ref not in included_refs:
                    excluded_refs.append(ref)

        context = self._build_context(included_sources, capability_payload)
        used_units = _canonical_units(context)
        if used_units > budget_units:
            raise ContextBudgetExceeded("compiled Context exceeds requested budget")

        if not coverage_complete:
            completeness = ContextCompleteness.INCOMPLETE
        elif excluded_refs:
            completeness = ContextCompleteness.TRUNCATED
        else:
            completeness = ContextCompleteness.COMPLETE

        receipt = ContextReceipt(
            context_receipt_id=f"{_CONTEXT_RECEIPT_PREFIX}{uuid4()}",
            program_id=program.program_id,
            program_revision=program.revision,
            included_refs=tuple(included_refs),
            excluded_refs=tuple(excluded_refs),
            completeness=completeness,
            budget_units=budget_units,
            created_at=utc_now(),
        )
        return self._contexts._store_compilation(receipt, context, used_units)
