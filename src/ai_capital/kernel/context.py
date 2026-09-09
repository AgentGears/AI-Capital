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
_COMPONENT_SCHEMA_VERSION = 10
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
_PROGRAM_CORRELATION_EVENT_PREFIXES = (
    "context.",
    "operation.",
    "completion.",
    "verification.",
)
_EVENT_STORAGE_OVERHEAD_LIMIT = 4096
_EVIDENCE_ADMISSION_STORAGE_LIMIT = 4096


def _identity_units(field_name: str, identity: str) -> int:
    return (
        _canonical_units({field_name: identity})
        - _canonical_units({field_name: ""})
    )


def _program_event_storage_limit(program_id: str, payload_units: int) -> int:
    return (
        payload_units
        + _EVENT_STORAGE_OVERHEAD_LIMIT
        + _identity_units("program_id", program_id)
    )


def _persisted_source_event_storage_limit(program_id: str, payload_units: int) -> int:
    identity_units = _identity_units("program_id", program_id)
    return payload_units + _EVENT_STORAGE_OVERHEAD_LIMIT + (2 * identity_units)


def _compiled_context_event_storage_limit(program_id: str, semantic_units: int) -> int:
    return (
        semantic_units
        + _EVENT_STORAGE_OVERHEAD_LIMIT
        + _identity_units("program_id", program_id)
    )


def _evidence_admission_storage_limit(evidence_id: str) -> int:
    return _EVIDENCE_ADMISSION_STORAGE_LIMIT + _identity_units("evidence_id", evidence_id)


def _evidence_event_storage_limit(
    evidence_id: str,
    evidence_units: int,
    admission_units: int,
) -> int:
    return (
        evidence_units
        + admission_units
        + _EVENT_STORAGE_OVERHEAD_LIMIT
        + _identity_units("evidence_id", evidence_id)
    )


def _correlation_identifies_program(event_type: str) -> bool:
    return event_type.startswith(_PROGRAM_CORRELATION_EVENT_PREFIXES)


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
    event_units: int


@dataclass(frozen=True, slots=True)
class _CurrentProgramPreflight:
    program_id: str
    program_revision: int
    source_ref: str
    projection_digest: str
    projection_units: int
    payload_units: int
    event_units: int
    last_sequence: int
    event_type: str
    event_digest: str


@dataclass(frozen=True, slots=True)
class _CapabilitySnapshotPreflight:
    source_ref: str
    snapshot_id: str
    snapshot_digest: str
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


def _persisted_source_projection_digest(
    *,
    sequence: int,
    event_id: str,
    program_id: str,
    program_revision: int,
    priority: str,
    source_digest: str,
    payload_units: int,
    event_digest: str,
) -> str:
    return canonical_digest(
        {
            "projection": "context.persisted_source_preflight",
            "sequence": sequence,
            "event_id": event_id,
            "program_id": program_id,
            "program_revision": program_revision,
            "priority": priority,
            "source_digest": source_digest,
            "payload_units": payload_units,
            "event_digest": event_digest,
        }
    )


def _persisted_source_event_metadata_digest(
    *,
    sequence: int,
    event_id: str,
    program_id: str,
    program_revision: int,
    priority: str,
    event_digest: str,
) -> str:
    return canonical_digest(
        {
            "projection": "context.persisted_source_event_metadata",
            "sequence": sequence,
            "event_id": event_id,
            "program_id": program_id,
            "program_revision": program_revision,
            "priority": priority,
            "event_digest": event_digest,
        }
    )


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


_MIN_HOST_CONTROL_SOURCE_UNITS = _canonical_units(
    _source_entry(
        _make_source(
            source_ref=event_ref("0" * 36),
            priority=ContextPriority.HOST_CONTROL,
            payload={},
        )
    )
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
            if version not in {None, 1, 2, 3, 4, 5, 6, 7, 8, 9, _COMPONENT_SCHEMA_VERSION}:
                raise IntegrityViolation(f"unsupported Context schema version {version}")

            event_columns = {
                str(column["name"])
                for column in self._host_store._db.execute(
                    "PRAGMA table_info(events)"
                ).fetchall()
            }
            for column_name, declaration in (
                ("context_source_program_id", "TEXT"),
                ("context_source_program_revision", "INTEGER"),
                ("context_source_priority", "TEXT"),
                ("context_source_metadata_digest", "TEXT"),
                ("context_recall_invalidated", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if column_name not in event_columns:
                    self._host_store._db.execute(
                        f"ALTER TABLE events ADD COLUMN {column_name} {declaration}"
                    )
            self._host_store._db.execute(
                """
                CREATE INDEX IF NOT EXISTS events_context_source_authority
                ON events(
                    event_type, context_source_program_id,
                    context_source_program_revision, context_source_priority, event_id
                )
                """
            )

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
                CREATE TABLE IF NOT EXISTS context_compiled_event_invalidations (
                    sequence INTEGER PRIMARY KEY,
                    event_id TEXT NOT NULL UNIQUE,
                    event_digest TEXT NOT NULL
                )
                """
            )
            self._host_store._db.execute(
                "DROP TRIGGER IF EXISTS context_receipt_projection_integrity_invalidate"
            )
            self._host_store._db.execute(
                """
                CREATE TRIGGER context_receipt_projection_integrity_invalidate
                AFTER UPDATE OF program_id, program_revision, compiled_event_id,
                                receipt_json, receipt_digest, context_json,
                                context_digest, used_units
                ON context_receipts
                WHEN OLD.program_id IS NOT NEW.program_id
                  OR OLD.program_revision IS NOT NEW.program_revision
                  OR OLD.compiled_event_id IS NOT NEW.compiled_event_id
                  OR OLD.receipt_json IS NOT NEW.receipt_json
                  OR OLD.receipt_digest IS NOT NEW.receipt_digest
                  OR OLD.context_json IS NOT NEW.context_json
                  OR OLD.context_digest IS NOT NEW.context_digest
                  OR OLD.used_units IS NOT NEW.used_units
                BEGIN
                    DELETE FROM context_receipt_event_index
                    WHERE context_receipt_id = OLD.context_receipt_id;
                END
                """
            )

            self._host_store._db.execute(
                """
                CREATE TABLE IF NOT EXISTS context_recall_event_index (
                    sequence INTEGER PRIMARY KEY,
                    event_id TEXT NOT NULL UNIQUE,
                    program_id TEXT,
                    correlation_id TEXT,
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
                    payload_json TEXT NOT NULL,
                    event_digest TEXT NOT NULL,
                    projection_digest TEXT NOT NULL,
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
                CREATE TABLE IF NOT EXISTS context_persisted_source_invalidations (
                    sequence INTEGER PRIMARY KEY,
                    event_id TEXT NOT NULL UNIQUE,
                    program_id TEXT NOT NULL,
                    program_revision INTEGER NOT NULL,
                    priority TEXT NOT NULL,
                    event_digest TEXT NOT NULL,
                    metadata_digest TEXT NOT NULL
                )
                """
            )
            self._host_store._db.execute(
                """
                CREATE INDEX IF NOT EXISTS context_persisted_source_invalidation_program
                ON context_persisted_source_invalidations(
                    program_id, program_revision, priority, sequence
                )
                """
            )
            self._host_store._db.execute(
                "DROP TRIGGER IF EXISTS context_persisted_source_event_content_invalidate"
            )
            self._host_store._db.execute(
                """
                CREATE TRIGGER context_persisted_source_event_content_invalidate
                AFTER UPDATE OF event_type, event_json, event_digest,
                                context_source_program_id,
                                context_source_program_revision,
                                context_source_priority,
                                context_source_metadata_digest ON events
                WHEN OLD.event_type = 'context.source_persisted'
                  OR NEW.event_type = 'context.source_persisted'
                BEGIN
                    INSERT OR IGNORE INTO context_persisted_source_invalidations(
                        sequence, event_id, program_id, program_revision, priority,
                        event_digest, metadata_digest
                    )
                    SELECT
                        OLD.sequence, OLD.event_id, OLD.context_source_program_id,
                        OLD.context_source_program_revision, OLD.context_source_priority,
                        OLD.event_digest, OLD.context_source_metadata_digest
                    WHERE OLD.event_type = 'context.source_persisted'
                      AND OLD.context_source_priority = 'host_control'
                      AND OLD.context_source_program_id IS NOT NULL
                      AND OLD.context_source_program_revision IS NOT NULL
                      AND OLD.context_source_metadata_digest IS NOT NULL
                      AND NOT (
                          OLD.event_type IS NEW.event_type
                          AND OLD.event_json IS NEW.event_json
                          AND OLD.event_digest IS NEW.event_digest
                          AND NEW.context_source_program_id IS NULL
                          AND NEW.context_source_program_revision IS NULL
                          AND NEW.context_source_priority IS NULL
                          AND NEW.context_source_metadata_digest IS NULL
                      )
                      AND (
                          OLD.event_type IS NOT NEW.event_type
                          OR OLD.event_json IS NOT NEW.event_json
                          OR OLD.event_digest IS NOT NEW.event_digest
                          OR OLD.context_source_program_id IS NOT NEW.context_source_program_id
                          OR OLD.context_source_program_revision IS NOT NEW.context_source_program_revision
                          OR OLD.context_source_priority IS NOT NEW.context_source_priority
                          OR OLD.context_source_metadata_digest IS NOT NEW.context_source_metadata_digest
                      );

                    UPDATE events
                    SET context_source_program_id = (
                            SELECT program_id FROM context_persisted_source_index
                            WHERE event_id = OLD.event_id
                        ),
                        context_source_program_revision = (
                            SELECT program_revision FROM context_persisted_source_index
                            WHERE event_id = OLD.event_id
                        ),
                        context_source_priority = (
                            SELECT priority FROM context_persisted_source_index
                            WHERE event_id = OLD.event_id
                        ),
                        context_source_metadata_digest = OLD.context_source_metadata_digest
                    WHERE sequence = OLD.sequence
                      AND OLD.event_type = 'context.source_persisted'
                      AND NEW.event_type = 'context.source_persisted'
                      AND OLD.event_json IS NEW.event_json
                      AND OLD.event_digest IS NEW.event_digest
                      AND NEW.context_source_program_id IS NULL
                      AND NEW.context_source_program_revision IS NULL
                      AND NEW.context_source_priority IS NULL
                      AND NEW.context_source_metadata_digest IS NULL
                      AND EXISTS (
                          SELECT 1 FROM context_persisted_source_index
                          WHERE event_id = OLD.event_id
                      );

                    DELETE FROM context_persisted_source_index
                    WHERE (sequence = OLD.sequence
                           OR event_id = OLD.event_id
                           OR sequence = NEW.sequence
                           OR event_id = NEW.event_id)
                      AND (
                          OLD.event_json IS NOT NEW.event_json
                          OR OLD.event_digest IS NOT NEW.event_digest
                      )
                      AND OLD.event_type IS NEW.event_type
                      AND OLD.context_source_program_id IS NEW.context_source_program_id
                      AND OLD.context_source_program_revision IS NEW.context_source_program_revision
                      AND OLD.context_source_priority IS NEW.context_source_priority;
                END
                """
            )

            persisted_source_columns = {
                str(column["name"])
                for column in self._host_store._db.execute(
                    "PRAGMA table_info(context_persisted_source_index)"
                ).fetchall()
            }
            if version == 1 and "projection_digest" not in persisted_source_columns:
                self._host_store._db.execute(
                    "ALTER TABLE context_persisted_source_index "
                    "ADD COLUMN projection_digest TEXT"
                )
                persisted_source_columns.add("projection_digest")
            if "projection_digest" not in persisted_source_columns:
                raise IntegrityViolation(
                    "persisted Context source schema lacks projection authentication"
                )
            if "payload_json" not in persisted_source_columns:
                self._host_store._db.execute(
                    "ALTER TABLE context_persisted_source_index ADD COLUMN payload_json TEXT"
                )
                persisted_source_columns.add("payload_json")
            if "payload_json" not in persisted_source_columns:
                raise IntegrityViolation(
                    "persisted Context source schema lacks bounded payload projection"
                )

            recall_event_columns = {
                str(column["name"])
                for column in self._host_store._db.execute(
                    "PRAGMA table_info(context_recall_event_index)"
                ).fetchall()
            }
            if "correlation_id" not in recall_event_columns:
                self._host_store._db.execute(
                    "ALTER TABLE context_recall_event_index ADD COLUMN correlation_id TEXT"
                )

            self._host_store._db.execute(
                "DROP TRIGGER IF EXISTS context_recall_event_index_insert"
            )
            self._host_store._db.execute(
                """
                CREATE TRIGGER context_recall_event_index_insert
                AFTER INSERT ON events
                BEGIN
                    INSERT INTO context_recall_event_index(
                        sequence, event_id, program_id, correlation_id,
                        event_type, event_digest
                    ) VALUES (
                        NEW.sequence,
                        NEW.event_id,
                        CASE
                            WHEN NEW.program_id IS NOT NULL THEN NEW.program_id
                            WHEN NEW.event_type LIKE 'context.%'
                              OR NEW.event_type LIKE 'operation.%'
                              OR NEW.event_type LIKE 'completion.%'
                              OR NEW.event_type LIKE 'verification.%'
                            THEN json_extract(NEW.event_json, '$.correlation_id')
                            ELSE NULL
                        END,
                        json_extract(NEW.event_json, '$.correlation_id'),
                        NEW.event_type,
                        NEW.event_digest
                    );
                END
                """
            )
            self._host_store._db.execute(
                "DROP TRIGGER IF EXISTS context_recall_event_integrity_invalidate"
            )
            self._host_store._db.execute(
                """
                CREATE TRIGGER context_recall_event_integrity_invalidate
                AFTER UPDATE OF event_json, event_digest, event_type, program_id ON events
                WHEN OLD.event_json IS NOT NEW.event_json
                  OR OLD.event_digest IS NOT NEW.event_digest
                  OR OLD.event_type IS NOT NEW.event_type
                  OR OLD.program_id IS NOT NEW.program_id
                BEGIN
                    INSERT OR IGNORE INTO context_compiled_event_invalidations(
                        sequence, event_id, event_digest
                    )
                    SELECT OLD.sequence, OLD.event_id, OLD.event_digest
                    WHERE OLD.event_type = 'context.compiled';

                    UPDATE events
                    SET context_recall_invalidated = 1
                    WHERE sequence = OLD.sequence;
                    DELETE FROM context_recall_event_index
                    WHERE event_id = OLD.event_id;
                END
                """
            )
            if version == 6:
                self._migrate_host_control_invalidations()
            self._rebuild_recall_event_index(
                authenticate_events=version is None or version < 8 or version == 9,
            )
            if version in {8, 9}:
                self._migrate_compiled_event_invalidations()
            self._rebuild_persisted_source_projection()

            if version is None:
                self._host_store._db.execute(
                    "INSERT INTO component_schema(component, version) VALUES (?, ?)",
                    (_COMPONENT, _COMPONENT_SCHEMA_VERSION),
                )
            elif version in {1, 2, 3, 4, 5, 6, 7, 8, 9}:
                self._host_store._db.execute(
                    "UPDATE component_schema SET version = ? WHERE component = ?",
                    (_COMPONENT_SCHEMA_VERSION, _COMPONENT),
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

    def _rebuild_recall_event_index(self, *, authenticate_events: bool) -> None:
        self._host_store._db.execute("DELETE FROM context_recall_event_index")
        last_sequence = 0
        while True:
            if authenticate_events:
                row = self._host_store._db.execute(
                    """
                    SELECT events.sequence, events.event_id, events.program_id,
                           events.event_type, events.event_json, events.event_digest,
                           events.context_recall_invalidated,
                           EXISTS (
                               SELECT 1
                               FROM context_persisted_source_invalidations AS invalidation
                               WHERE invalidation.event_id = events.event_id
                           ) AS persisted_source_invalidated
                    FROM events
                    WHERE events.sequence > ?
                    ORDER BY events.sequence
                    LIMIT 1
                    """,
                    (last_sequence,),
                ).fetchone()
            else:
                row = self._host_store._db.execute(
                    """
                    SELECT sequence, event_id, program_id, event_type, event_digest,
                           json_extract(event_json, '$.correlation_id')
                               AS semantic_correlation_id
                    FROM events
                    WHERE sequence > ?
                    ORDER BY sequence
                    LIMIT 1
                    """,
                    (last_sequence,),
                ).fetchone()
            if row is None:
                break

            if authenticate_events:
                try:
                    sequence = int(row["sequence"])
                    invalidated = int(row["context_recall_invalidated"])
                    persisted_source_invalidated = int(row["persisted_source_invalidated"])
                except (TypeError, ValueError) as exc:
                    raise IntegrityViolation(
                        "Context Event recall-index migration metadata is malformed"
                    ) from exc
                last_sequence = sequence
                if invalidated != 0 or persisted_source_invalidated != 0:
                    continue
                event = self._decode_event_row(row)
                event_id = event.event_id
                program_id = event.program_id
                correlation_id = event.correlation_id
                event_type = event.event_type
                event_digest = event.digest
            else:
                try:
                    sequence = int(row["sequence"])
                except (TypeError, ValueError) as exc:
                    raise IntegrityViolation(
                        "Context Event recall-index metadata is malformed"
                    ) from exc
                event_id = row["event_id"]
                program_id = row["program_id"]
                correlation_id = row["semantic_correlation_id"]
                event_type = row["event_type"]
                event_digest = row["event_digest"]
                if (
                    type(event_id) is not str
                    or not event_id.strip()
                    or (program_id is not None and type(program_id) is not str)
                    or (correlation_id is not None and type(correlation_id) is not str)
                    or type(event_type) is not str
                    or not event_type.strip()
                    or type(event_digest) is not str
                    or not event_digest.strip()
                ):
                    raise IntegrityViolation(
                        "Context Event recall-index metadata is invalid"
                    )
                last_sequence = sequence

            semantic_program_id = program_id
            if semantic_program_id is None and _correlation_identifies_program(event_type):
                semantic_program_id = correlation_id
            try:
                self._host_store._db.execute(
                    """
                    INSERT INTO context_recall_event_index(
                        sequence, event_id, program_id, correlation_id,
                        event_type, event_digest
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sequence,
                        event_id,
                        semantic_program_id,
                        correlation_id,
                        event_type,
                        event_digest,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise IntegrityViolation(
                    "Context Event recall-index rebuild collided"
                ) from exc

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

    def _migrate_host_control_invalidations(self) -> None:
        last_sequence = 0
        while True:
            row = self._host_store._db.execute(
                """
                SELECT
                    idx.sequence AS indexed_sequence,
                    idx.event_id AS indexed_event_id,
                    idx.program_id AS indexed_program_id,
                    idx.program_revision AS indexed_program_revision,
                    idx.priority AS indexed_priority,
                    idx.source_digest AS indexed_source_digest,
                    idx.payload_units AS indexed_payload_units,
                    idx.event_digest AS indexed_event_digest,
                    idx.projection_digest AS indexed_projection_digest,
                    events.sequence AS semantic_sequence,
                    events.event_type AS semantic_event_type,
                    events.context_source_program_id AS semantic_program_id,
                    events.context_source_program_revision AS semantic_program_revision,
                    events.context_source_priority AS semantic_priority,
                    events.event_digest AS semantic_event_digest
                FROM context_persisted_source_index AS idx
                LEFT JOIN events ON events.event_id = idx.event_id
                WHERE idx.priority = ? AND idx.sequence > ?
                ORDER BY idx.sequence
                LIMIT 1
                """,
                (ContextPriority.HOST_CONTROL.value, last_sequence),
            ).fetchone()
            if row is None:
                break
            try:
                sequence = int(row["indexed_sequence"])
                program_revision = int(row["indexed_program_revision"])
                payload_units = int(row["indexed_payload_units"])
            except (TypeError, ValueError) as exc:
                raise IntegrityViolation(
                    "Host-control projection migration metadata is malformed"
                ) from exc
            last_sequence = sequence
            event_id = row["indexed_event_id"]
            program_id = row["indexed_program_id"]
            priority = row["indexed_priority"]
            source_digest = row["indexed_source_digest"]
            event_digest = row["indexed_event_digest"]
            projection_digest = row["indexed_projection_digest"]
            if (
                type(event_id) is not str
                or not event_id.strip()
                or type(program_id) is not str
                or not program_id.strip()
                or priority != ContextPriority.HOST_CONTROL.value
                or type(source_digest) is not str
                or not source_digest.strip()
                or type(event_digest) is not str
                or not event_digest.strip()
                or type(projection_digest) is not str
                or not projection_digest.strip()
                or program_revision < 0
                or payload_units < _canonical_units({})
            ):
                raise IntegrityViolation(
                    "Host-control projection migration metadata is invalid"
                )
            expected_projection_digest = _persisted_source_projection_digest(
                sequence=sequence,
                event_id=event_id,
                program_id=program_id,
                program_revision=program_revision,
                priority=priority,
                source_digest=source_digest,
                payload_units=payload_units,
                event_digest=event_digest,
            )
            if projection_digest != expected_projection_digest:
                raise IntegrityViolation(
                    "Host-control projection migration authentication mismatch"
                )
            semantic_diverged = (
                row["semantic_sequence"] is None
                or int(row["semantic_sequence"]) != sequence
                or row["semantic_event_type"] != "context.source_persisted"
                or row["semantic_program_id"] != program_id
                or row["semantic_program_revision"] != program_revision
                or row["semantic_priority"] != priority
                or row["semantic_event_digest"] != event_digest
            )
            if not semantic_diverged:
                continue
            metadata_digest = _persisted_source_event_metadata_digest(
                sequence=sequence,
                event_id=event_id,
                program_id=program_id,
                program_revision=program_revision,
                priority=priority,
                event_digest=event_digest,
            )
            self._host_store._db.execute(
                """
                INSERT OR IGNORE INTO context_persisted_source_invalidations(
                    sequence, event_id, program_id, program_revision, priority,
                    event_digest, metadata_digest
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sequence, event_id, program_id, program_revision, priority,
                    event_digest, metadata_digest,
                ),
            )

    def _migrate_compiled_event_invalidations(self) -> None:
        last_sequence = 0
        while True:
            row = self._host_store._db.execute(
                """
                SELECT events.sequence, events.event_id, events.event_digest,
                       events.context_recall_invalidated,
                       EXISTS (
                           SELECT 1 FROM context_receipts AS receipt
                           WHERE receipt.compiled_event_id = events.event_id
                       ) AS has_receipt,
                       EXISTS (
                           SELECT 1 FROM context_receipt_event_index AS receipt_index
                           WHERE receipt_index.event_id = events.event_id
                       ) AS has_receipt_index
                FROM events
                WHERE events.sequence > ?
                  AND events.context_recall_invalidated != 0
                ORDER BY events.sequence
                LIMIT 1
                """,
                (last_sequence,),
            ).fetchone()
            if row is None:
                break
            try:
                sequence = int(row["sequence"])
                invalidated = int(row["context_recall_invalidated"])
                has_receipt = int(row["has_receipt"])
                has_receipt_index = int(row["has_receipt_index"])
            except (TypeError, ValueError) as exc:
                raise IntegrityViolation(
                    "compiled Context Event migration metadata is malformed"
                ) from exc
            last_sequence = sequence
            event_id = row["event_id"]
            event_digest = row["event_digest"]
            if (
                invalidated == 0
                or has_receipt not in {0, 1}
                or has_receipt_index not in {0, 1}
                or type(event_id) is not str
                or not event_id.strip()
                or type(event_digest) is not str
                or not event_digest.strip()
            ):
                raise IntegrityViolation(
                    "compiled Context Event migration metadata is invalid"
                )

            if not has_receipt and not has_receipt_index:
                envelope_row = self._host_store._db.execute(
                    "SELECT event_json FROM events WHERE sequence = ?",
                    (sequence,),
                ).fetchone()
                if envelope_row is None:
                    raise IntegrityViolation(
                        "legacy invalidated Event envelope is missing"
                    )
                try:
                    legacy_event = record_from_json(Event, envelope_row["event_json"])
                except (TypeError, ValueError) as exc:
                    raise IntegrityViolation(
                        "legacy invalidated Event envelope cannot be decoded"
                    ) from exc
                if not isinstance(legacy_event, Event):
                    raise IntegrityViolation(
                        "legacy invalidated Event envelope decoded wrong type"
                    )
                if (
                    legacy_event.sequence != sequence
                    or legacy_event.event_id != event_id
                    or legacy_event.digest != event_digest
                    or not verify_event_digest(legacy_event)
                ):
                    raise IntegrityViolation(
                        "legacy invalidated Event envelope authentication mismatch"
                    )
                if legacy_event.event_type != "context.compiled":
                    raise IntegrityViolation(
                        "legacy invalidated Event cannot be classified safely"
                    )

            self._host_store._db.execute(
                """
                INSERT OR IGNORE INTO context_compiled_event_invalidations(
                    sequence, event_id, event_digest
                ) VALUES (?, ?, ?)
                """,
                (sequence, event_id, event_digest),
            )

    def _rebuild_persisted_source_projection(self) -> None:
        self._host_store._db.execute("DELETE FROM context_persisted_source_index")
        last_sequence = 0
        while True:
            row = self._host_store._db.execute(
                """
                SELECT sequence, event_id, program_id, event_type, event_json, event_digest
                FROM events
                WHERE event_type = 'context.source_persisted' AND sequence > ?
                ORDER BY sequence
                LIMIT 1
                """,
                (last_sequence,),
            ).fetchone()
            if row is None:
                break
            event = self._decode_event_row(row)
            last_sequence = event.sequence
            if not event.correlation_id:
                raise IntegrityViolation("persisted Context source Event lacks Program binding")
            persisted, _ = self._source_from_persisted_event(
                event,
                expected_program_id=event.correlation_id,
            )
            payload_json = canonical_json(persisted.payload)
            payload_units = len(payload_json.encode("utf-8"))
            projection_digest = _persisted_source_projection_digest(
                sequence=event.sequence,
                event_id=event.event_id,
                program_id=persisted.program_id,
                program_revision=persisted.program_revision,
                priority=persisted.priority.value,
                source_digest=persisted.source_digest,
                payload_units=payload_units,
                event_digest=event.digest,
            )
            event_metadata_digest = _persisted_source_event_metadata_digest(
                sequence=event.sequence,
                event_id=event.event_id,
                program_id=persisted.program_id,
                program_revision=persisted.program_revision,
                priority=persisted.priority.value,
                event_digest=event.digest,
            )
            self._host_store._db.execute(
                """
                UPDATE events
                SET context_source_program_id = ?,
                    context_source_program_revision = ?,
                    context_source_priority = ?,
                    context_source_metadata_digest = ?
                WHERE sequence = ?
                """,
                (persisted.program_id, persisted.program_revision, persisted.priority.value,
                 event_metadata_digest, event.sequence),
            )
            try:
                self._host_store._db.execute(
                    """
                    INSERT INTO context_persisted_source_index(
                        sequence, event_id, program_id, program_revision, priority,
                        source_digest, payload_units, payload_json, event_digest, projection_digest
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.sequence,
                        event.event_id,
                        persisted.program_id,
                        persisted.program_revision,
                        persisted.priority.value,
                        persisted.source_digest,
                        payload_units,
                        payload_json,
                        event.digest,
                        projection_digest,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise IntegrityViolation(
                    "persisted Context source projection rebuild collided"
                ) from exc

    def _iter_semantic_receipts(self):
        last_sequence = 0
        while True:
            row = self._host_store._db.execute(
                """
                SELECT sequence, event_id, program_id, event_type, event_json, event_digest,
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
            receipt, context, used_units = self._decode_compiled_event(event)
            last_sequence = event.sequence
            yield event, receipt, context, used_units

    def _semantic_receipts(self):
        """Compatibility hook exposing the bounded semantic receipt iterator."""
        return self._iter_semantic_receipts()

    def _reject_invalidated_compiled_receipt_events(self) -> None:
        invalidated = self._host_store._db.execute(
            """
            SELECT event_id
            FROM context_compiled_event_invalidations
            ORDER BY sequence
            LIMIT 1
            """
        ).fetchone()
        if invalidated is not None:
            raise IntegrityViolation("compiled Context Event was invalidated")
        invalidated = self._host_store._db.execute(
            """
            SELECT event.event_id
            FROM context_receipt_event_index AS receipt_index
            JOIN events AS event ON event.event_id = receipt_index.event_id
            WHERE event.context_recall_invalidated != 0
            ORDER BY receipt_index.sequence
            LIMIT 1
            """
        ).fetchone()
        if invalidated is not None:
            raise IntegrityViolation("compiled Context Event was invalidated")

    def _rebuild_receipt_projection(self) -> None:
        self._reject_invalidated_compiled_receipt_events()
        self._host_store._db.execute("DELETE FROM context_receipt_event_index")
        self._host_store._db.execute("DELETE FROM context_receipts")
        for event, receipt, context, used_units in self._iter_semantic_receipts():
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
        semantic_count = int(
            self._host_store._db.execute(
                "SELECT COUNT(*) AS count FROM events WHERE event_type = 'context.compiled'"
            ).fetchone()["count"]
        )
        receipt_count = int(
            self._host_store._db.execute(
                "SELECT COUNT(*) AS count FROM context_receipts"
            ).fetchone()["count"]
        )
        index_count = int(
            self._host_store._db.execute(
                "SELECT COUNT(*) AS count FROM context_receipt_event_index"
            ).fetchone()["count"]
        )
        if semantic_count != receipt_count or semantic_count != index_count:
            raise IntegrityViolation(
                "Context receipt records/index diverge from semantic Events"
            )

        seen = 0
        for event, receipt, context, used_units in self._iter_semantic_receipts():
            seen += 1
            receipt_row = self._host_store._db.execute(
                """
                SELECT program_id, compiled_event_id
                FROM context_receipts WHERE context_receipt_id = ?
                """,
                (receipt.context_receipt_id,),
            ).fetchone()
            index_row = self._host_store._db.execute(
                """
                SELECT sequence, program_id, event_id
                FROM context_receipt_event_index WHERE context_receipt_id = ?
                """,
                (receipt.context_receipt_id,),
            ).fetchone()
            if (
                receipt_row is None
                or index_row is None
                or receipt_row["program_id"] != receipt.program_id
                or receipt_row["compiled_event_id"] != event.event_id
                or int(index_row["sequence"]) != event.sequence
                or index_row["program_id"] != receipt.program_id
                or index_row["event_id"] != event.event_id
            ):
                raise IntegrityViolation(
                    "Context receipt records/index diverge from semantic Events"
                )
            durable = self.get(receipt.context_receipt_id)
            if (
                durable.receipt != receipt
                or durable.context != context
                or durable.used_units != used_units
            ):
                raise IntegrityViolation("ContextReceipt diverges from semantic Event")
        if seen != semantic_count:
            raise IntegrityViolation(
                "Context receipt records/index diverge from semantic Events"
            )

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
            payload_json = canonical_json(source.payload)
            payload_units = len(payload_json.encode("utf-8"))
            projection_digest = _persisted_source_projection_digest(
                sequence=event.sequence,
                event_id=event.event_id,
                program_id=source.program_id,
                program_revision=source.program_revision,
                priority=source.priority.value,
                source_digest=source.source_digest,
                payload_units=payload_units,
                event_digest=event.digest,
            )
            event_metadata_digest = _persisted_source_event_metadata_digest(
                sequence=event.sequence,
                event_id=event.event_id,
                program_id=source.program_id,
                program_revision=source.program_revision,
                priority=source.priority.value,
                event_digest=event.digest,
            )
            self._host_store._db.execute(
                """
                UPDATE events
                SET context_source_program_id = ?,
                    context_source_program_revision = ?,
                    context_source_priority = ?,
                    context_source_metadata_digest = ?
                WHERE sequence = ?
                """,
                (source.program_id, source.program_revision, source.priority.value,
                 event_metadata_digest, event.sequence),
            )
            try:
                self._host_store._db.execute(
                    """
                    INSERT INTO context_persisted_source_index(
                        sequence, event_id, program_id, program_revision, priority,
                        source_digest, payload_units, payload_json, event_digest, projection_digest
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event.sequence,
                        event.event_id,
                        source.program_id,
                        source.program_revision,
                        source.priority.value,
                        source.source_digest,
                        payload_units,
                        payload_json,
                        event.digest,
                        projection_digest,
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
                length(CAST(events.event_json AS BLOB)) AS semantic_event_units,
                context_persisted_source_index.sequence AS indexed_sequence,
                context_persisted_source_index.event_id AS indexed_event_id,
                context_persisted_source_index.program_id AS indexed_program_id,
                context_persisted_source_index.program_revision AS indexed_program_revision,
                context_persisted_source_index.priority AS indexed_priority,
                context_persisted_source_index.source_digest AS indexed_source_digest,
                context_persisted_source_index.payload_units AS indexed_payload_units,
                length(CAST(context_persisted_source_index.payload_json AS BLOB)) AS indexed_payload_json_units,
                context_persisted_source_index.event_digest AS indexed_event_digest,
                context_persisted_source_index.projection_digest AS indexed_projection_digest
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
        try:
            indexed_sequence = int(row["indexed_sequence"])
            event_sequence = int(row["event_sequence"])
            indexed_program_revision = int(row["indexed_program_revision"])
            indexed_payload_units = int(row["indexed_payload_units"])
            indexed_payload_json_units = int(row["indexed_payload_json_units"])
            semantic_event_units = int(row["semantic_event_units"])
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("persisted Context source metadata is malformed") from exc
        indexed_event_id = row["indexed_event_id"]
        indexed_program_id = row["indexed_program_id"]
        indexed_priority = row["indexed_priority"]
        indexed_source_digest = row["indexed_source_digest"]
        indexed_event_digest = row["indexed_event_digest"]
        indexed_projection_digest = row["indexed_projection_digest"]
        if (
            row["event_type"] != "context.source_persisted"
            or indexed_sequence != event_sequence
            or indexed_event_id != event_id
            or indexed_event_digest != row["semantic_event_digest"]
        ):
            raise IntegrityViolation(
                "persisted Context source metadata diverges from Event row"
            )
        if (
            type(indexed_program_id) is not str
            or not indexed_program_id.strip()
            or type(indexed_priority) is not str
            or type(indexed_source_digest) is not str
            or not indexed_source_digest.strip()
            or type(indexed_event_digest) is not str
            or not indexed_event_digest.strip()
            or type(indexed_projection_digest) is not str
            or not indexed_projection_digest.strip()
            or indexed_program_revision < 0
            or indexed_payload_units < _canonical_units({})
            or indexed_payload_json_units != indexed_payload_units
            or semantic_event_units <= 0
        ):
            raise IntegrityViolation("persisted Context source metadata is invalid")
        if semantic_event_units > _persisted_source_event_storage_limit(
            indexed_program_id, indexed_payload_units
        ):
            raise IntegrityViolation(
                "persisted Context source Event storage exceeds bounded semantic envelope"
            )
        expected_projection_digest = _persisted_source_projection_digest(
            sequence=indexed_sequence,
            event_id=indexed_event_id,
            program_id=indexed_program_id,
            program_revision=indexed_program_revision,
            priority=indexed_priority,
            source_digest=indexed_source_digest,
            payload_units=indexed_payload_units,
            event_digest=indexed_event_digest,
        )
        if indexed_projection_digest != expected_projection_digest:
            raise IntegrityViolation(
                "persisted Context source metadata authentication mismatch"
            )
        if indexed_program_id != program_id:
            raise InvalidRequest("persisted Context source belongs to a different Program")
        try:
            priority = ContextPriority(indexed_priority)
        except ValueError as exc:
            raise IntegrityViolation("persisted Context source priority is invalid") from exc
        program_revision = indexed_program_revision
        payload_units = indexed_payload_units
        source_digest = indexed_source_digest
        if priority not in _PERSISTABLE_PRIORITIES:
            raise IntegrityViolation("persisted Context source metadata is invalid")
        return _PersistedSourcePreflight(
            source_ref=source_ref,
            program_id=program_id,
            program_revision=program_revision,
            priority=priority,
            source_digest=source_digest,
            payload_units=payload_units,
            event_units=semantic_event_units,
        )

    def _current_host_control_refs(
        self,
        program_id: str,
        program_revision: int,
        *,
        max_refs: int | None = None,
    ) -> tuple[str, ...]:
        if max_refs is not None and max_refs < 0:
            raise InvalidRequest("Host-control enumeration bound cannot be negative")
        invalidation = self._host_store._db.execute(
            """
            SELECT sequence, event_id, program_id, program_revision, priority,
                   event_digest, metadata_digest
            FROM context_persisted_source_invalidations
            WHERE program_id = ? AND program_revision = ? AND priority = ?
            ORDER BY sequence
            LIMIT 1
            """,
            (program_id, program_revision, ContextPriority.HOST_CONTROL.value),
        ).fetchone()
        if invalidation is not None:
            try:
                invalidation_sequence = int(invalidation["sequence"])
                invalidation_revision = int(invalidation["program_revision"])
            except (TypeError, ValueError) as exc:
                raise IntegrityViolation(
                    "Host-control invalidation evidence is malformed"
                ) from exc
            invalidation_event_id = invalidation["event_id"]
            invalidation_program_id = invalidation["program_id"]
            invalidation_priority = invalidation["priority"]
            invalidation_event_digest = invalidation["event_digest"]
            invalidation_metadata_digest = invalidation["metadata_digest"]
            if (
                type(invalidation_event_id) is not str
                or not invalidation_event_id.strip()
                or invalidation_program_id != program_id
                or invalidation_revision != program_revision
                or invalidation_priority != ContextPriority.HOST_CONTROL.value
                or type(invalidation_event_digest) is not str
                or not invalidation_event_digest.strip()
                or type(invalidation_metadata_digest) is not str
                or not invalidation_metadata_digest.strip()
            ):
                raise IntegrityViolation(
                    "Host-control invalidation evidence is invalid"
                )
            expected_invalidation_digest = _persisted_source_event_metadata_digest(
                sequence=invalidation_sequence,
                event_id=invalidation_event_id,
                program_id=program_id,
                program_revision=program_revision,
                priority=ContextPriority.HOST_CONTROL.value,
                event_digest=invalidation_event_digest,
            )
            if invalidation_metadata_digest != expected_invalidation_digest:
                raise IntegrityViolation(
                    "Host-control invalidation evidence authentication mismatch"
                )
            raise IntegrityViolation(
                "current Host-control source has durable invalidation evidence"
            )
        semantic_cursor = self._host_store._db.execute(
            """
            SELECT sequence, event_id, event_digest,
                   context_source_program_id, context_source_program_revision,
                   context_source_priority, context_source_metadata_digest
            FROM events
            WHERE event_type = 'context.source_persisted'
              AND context_source_program_id = ?
              AND context_source_program_revision = ?
              AND context_source_priority = ?
            ORDER BY event_id
            """,
            (program_id, program_revision, ContextPriority.HOST_CONTROL.value),
        )
        projected_cursor = self._host_store._db.execute(
            """
            SELECT event_id
            FROM context_persisted_source_index
            WHERE program_id = ? AND program_revision = ? AND priority = ?
            ORDER BY event_id
            """,
            (program_id, program_revision, ContextPriority.HOST_CONTROL.value),
        )
        refs: list[str] = []
        while True:
            semantic_row = semantic_cursor.fetchone()
            projected_row = projected_cursor.fetchone()
            if semantic_row is None or projected_row is None:
                if semantic_row is not None or projected_row is not None:
                    raise IntegrityViolation(
                        "Host-control projection coverage diverges from semantic Events"
                    )
                break
            if max_refs is not None and len(refs) >= max_refs:
                raise ContextBudgetExceeded(
                    "Host-control set exceeds bounded enumeration capacity"
                )
            metadata_digest = semantic_row["context_source_metadata_digest"]
            if type(metadata_digest) is not str or not metadata_digest.strip():
                raise IntegrityViolation("Host-control Event metadata is incomplete")
            expected_metadata_digest = _persisted_source_event_metadata_digest(
                sequence=int(semantic_row["sequence"]),
                event_id=str(semantic_row["event_id"]),
                program_id=str(semantic_row["context_source_program_id"]),
                program_revision=int(semantic_row["context_source_program_revision"]),
                priority=str(semantic_row["context_source_priority"]),
                event_digest=str(semantic_row["event_digest"]),
            )
            if metadata_digest != expected_metadata_digest:
                raise IntegrityViolation(
                    "Host-control Event metadata authentication mismatch"
                )
            event_id = str(semantic_row["event_id"])
            if event_id != str(projected_row["event_id"]):
                raise IntegrityViolation(
                    "Host-control projection coverage diverges from semantic Events"
                )
            refs.append(event_ref(event_id))
        return tuple(refs)

    def _materialize_persisted_source(
        self,
        program_id: str,
        preflight: _PersistedSourcePreflight,
    ) -> ContextSource:
        event_id = preflight.source_ref[len(_EVENT_REF_PREFIX) :]
        row = self._host_store._db.execute(
            """
            SELECT program_id, program_revision, priority, source_digest,
                   payload_units, payload_json
            FROM context_persisted_source_index
            WHERE event_id = ?
            """,
            (event_id,),
        ).fetchone()
        if row is None:
            raise IntegrityViolation(
                "persisted Context source payload projection is missing"
            )
        try:
            program_revision = int(row["program_revision"])
            payload_units = int(row["payload_units"])
            payload = freeze_json(json.loads(row["payload_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise IntegrityViolation(
                "persisted Context source payload projection is malformed"
            ) from exc
        if not isinstance(payload, FrozenMap):
            raise IntegrityViolation(
                "persisted Context source payload projection is not an object"
            )
        if (
            row["program_id"] != program_id
            or row["program_id"] != preflight.program_id
            or program_revision != preflight.program_revision
            or row["priority"] != preflight.priority.value
            or row["source_digest"] != preflight.source_digest
            or payload_units != preflight.payload_units
            or _canonical_units(payload) != preflight.payload_units
            or canonical_digest(payload) != preflight.source_digest
        ):
            raise IntegrityViolation(
                "persisted Context source payload projection diverges from preflight"
            )
        return _make_source(
            source_ref=preflight.source_ref,
            priority=preflight.priority,
            payload=payload,
        )

    def _current_program_preflight(self, program_id: str) -> _CurrentProgramPreflight:
        row = self._host_store._db.execute(
            """
            SELECT
                program_projections.program_id AS projected_program_id,
                program_projections.revision AS projected_revision,
                program_projections.projection_digest AS projection_digest,
                length(CAST(program_projections.projection_json AS BLOB)) AS projection_units,
                program_projections.last_sequence AS last_sequence,
                events.event_id AS event_id,
                events.program_id AS event_program_id,
                events.event_type AS event_type,
                events.event_digest AS event_digest,
                length(CAST(events.event_json AS BLOB)) AS event_units
            FROM program_projections
            JOIN events ON events.sequence = program_projections.last_sequence
            WHERE program_projections.program_id = ?
            """,
            (program_id,),
        ).fetchone()
        if row is None:
            raise InvalidRequest(f"unknown Program: {program_id}")
        try:
            revision = int(row["projected_revision"])
            projection_units = int(row["projection_units"])
            last_sequence = int(row["last_sequence"])
            event_units = int(row["event_units"])
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("current Program preflight metadata is malformed") from exc
        projection_digest = row["projection_digest"]
        event_id = row["event_id"]
        event_type = row["event_type"]
        event_digest = row["event_digest"]
        if (
            row["projected_program_id"] != program_id
            or row["event_program_id"] != program_id
            or revision < 0
            or projection_units <= 0
            or event_units <= 0
            or last_sequence <= 0
            or type(projection_digest) is not str
            or not projection_digest.strip()
            or type(event_id) is not str
            or not event_id.strip()
            or type(event_type) is not str
            or not event_type.startswith("program.")
            or type(event_digest) is not str
            or not event_digest.strip()
        ):
            raise IntegrityViolation("current Program preflight metadata is invalid")
        payload_units = (
            projection_units
            + _canonical_units({"program": {}})
            - _canonical_units({})
        )
        return _CurrentProgramPreflight(
            program_id=program_id,
            program_revision=revision,
            source_ref=event_ref(event_id),
            projection_digest=projection_digest,
            projection_units=projection_units,
            payload_units=payload_units,
            event_units=event_units,
            last_sequence=last_sequence,
            event_type=event_type,
            event_digest=event_digest,
        )

    def current_program_source(
        self,
        program_id: str,
        *,
        preflight: _CurrentProgramPreflight | None = None,
    ) -> ContextSource:
        preflight = (
            self._current_program_preflight(program_id)
            if preflight is None
            else preflight
        )
        if preflight.program_id != program_id:
            raise InvalidRequest("current Program preflight belongs to a different Program")
        program = self._host_store.get(program_id)
        if (
            program.revision != preflight.program_revision
            or canonical_digest(program) != preflight.projection_digest
        ):
            raise StaleProgramRevision("Program changed during Context compilation")
        event_row = self._host_store._db.execute(
            """
            SELECT sequence, event_id, program_id, event_type, event_json, event_digest
            FROM events WHERE sequence = ?
            """,
            (preflight.last_sequence,),
        ).fetchone()
        if event_row is None:
            raise IntegrityViolation("current Program projection lacks semantic Event")
        event = self._decode_event_row(event_row)
        if (
            event.program_id != program_id
            or event_ref(event.event_id) != preflight.source_ref
            or event.event_type != preflight.event_type
            or event.digest != preflight.event_digest
        ):
            raise IntegrityViolation("current Program Event diverges from preflight metadata")
        try:
            payload = event.payload["program"]
        except KeyError as exc:
            raise IntegrityViolation("current Program Event lacks Program snapshot") from exc
        if not isinstance(payload, FrozenMap):
            raise IntegrityViolation("current Program Event snapshot is malformed")
        if canonical_json(payload) != canonical_json(to_canonical_data(program)):
            raise IntegrityViolation("current Program Event differs from Program projection")
        source = _make_source(
            source_ref=event_ref(event.event_id),
            priority=ContextPriority.CURRENT_PROGRAM,
            payload={"program": payload},
        )
        if _canonical_units(source.payload) != preflight.payload_units:
            raise IntegrityViolation("current Program materialization diverges from size preflight")
        return source

    def _event_belongs_to_program(self, event: Event, program_id: str) -> bool:
        return event.program_id == program_id or (
            _correlation_identifies_program(event.event_type)
            and event.correlation_id == program_id
        )

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
        admission = self._evidence.admission(evidence_id)
        artifact = self._evidence.artifact(evidence_id)
        return _make_source(
            source_ref=source_ref,
            priority=ContextPriority.RECALLED_HISTORY,
            payload={
                "evidence": to_canonical_data(evidence),
                "admission": to_canonical_data(admission),
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
                    events.context_recall_invalidated AS event_invalidated,
                    context_recall_event_index.sequence AS indexed_sequence,
                    context_recall_event_index.program_id AS indexed_program_id,
                    context_recall_event_index.correlation_id AS indexed_correlation_id,
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
            try:
                invalidated = int(row["event_invalidated"])
            except (TypeError, ValueError) as exc:
                raise IntegrityViolation("Event recall invalidation metadata is malformed") from exc
            if invalidated != 0:
                raise IntegrityViolation("Event recall source was invalidated")
            if row["indexed_sequence"] is None:
                raise IntegrityViolation("Event recall address lacks durable metadata index")
            scoped_correlation = (
                row["event_program_id"] is None
                and _correlation_identifies_program(str(row["event_type"]))
            )
            if (
                int(row["indexed_sequence"]) != int(row["sequence"])
                or row["indexed_event_type"] != row["event_type"]
                or row["indexed_event_digest"] != row["event_digest"]
                or (
                    row["event_program_id"] is not None
                    and row["indexed_program_id"] != row["event_program_id"]
                )
                or (
                    scoped_correlation
                    and (
                        row["indexed_correlation_id"] is None
                        or row["indexed_program_id"] != row["indexed_correlation_id"]
                    )
                )
            ):
                raise IntegrityViolation("Event recall metadata index diverges from Event row")
            if row["event_program_id"] is None and not scoped_correlation:
                raise InvalidRequest("historical Event has no Program ownership binding")
            expected_program_id = (
                row["event_program_id"]
                if row["event_program_id"] is not None
                else row["indexed_correlation_id"]
            )
            if expected_program_id != program_id:
                raise InvalidRequest("historical Event belongs to a different Program")
            if row["event_type"] == "context.source_persisted":
                self._persisted_source_preflight(program_id, source_ref)
            return
        if source_ref.startswith(_EVIDENCE_REF_PREFIX):
            if self._evidence is None:
                raise InvalidRequest("Evidence recall requires the Host Evidence repository")
            evidence_id = source_ref[len(_EVIDENCE_REF_PREFIX) :]
            metadata = self._evidence._metadata_row(evidence_id)
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
                (metadata["admitted_event_id"],),
            ).fetchone()
            if (
                indexed is None
                or int(indexed["indexed_sequence"]) != int(indexed["event_sequence"])
                or indexed["indexed_evidence_id"] != metadata["evidence_id"]
                or indexed["indexed_event_type"] != "evidence.admitted"
                or indexed["semantic_event_id"] != metadata["admitted_event_id"]
                or indexed["semantic_event_type"] != "evidence.admitted"
            ):
                raise IntegrityViolation("Evidence recall Event binding mismatch")
            artifact_digest = str(metadata["artifact_digest"])
            self._evidence._artifact_preflight(
                artifact_digest,
                expected_content_ref=self._evidence._content_ref(artifact_digest),
            )
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
            semantic = self._host_store._db.execute(
                """
                SELECT event_type, context_recall_invalidated
                FROM events WHERE event_id = ?
                """,
                (row["compiled_event_id"],),
            ).fetchone()
            if (
                index is None
                or index["program_id"] != row["program_id"]
                or index["event_id"] != row["compiled_event_id"]
                or semantic is None
                or semantic["event_type"] != "context.compiled"
            ):
                raise IntegrityViolation("ContextReceipt lacks valid semantic Event binding")
            try:
                semantic_invalidated = int(semantic["context_recall_invalidated"])
            except (TypeError, ValueError) as exc:
                raise IntegrityViolation(
                    "ContextReceipt semantic Event invalidation metadata is malformed"
                ) from exc
            if semantic_invalidated != 0:
                raise IntegrityViolation("ContextReceipt semantic Event was invalidated")
            if row["program_id"] != program_id:
                raise InvalidRequest("historical Context belongs to a different Program")
            return
        raise InvalidRequest(f"unsupported durable recall address: {source_ref}")

    def _resolve_recall(self, program_id: str, source_ref: str) -> ContextSource:
        if source_ref.startswith(_EVENT_REF_PREFIX):
            event_id = source_ref[len(_EVENT_REF_PREFIX) :]
            persisted = self._host_store._db.execute(
                "SELECT event_id FROM context_persisted_source_index WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if persisted is not None:
                preflight = self._persisted_source_preflight(program_id, source_ref)
                projected = self._materialize_persisted_source(program_id, preflight)
                return _make_source(
                    source_ref=source_ref,
                    priority=ContextPriority.RECALLED_HISTORY,
                    payload=projected.payload,
                )
            event = self._event_by_id(event_id)
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

    def _event_storage_units(self, event_id: str) -> int:
        row = self._host_store._db.execute(
            """
            SELECT length(CAST(event_json AS BLOB)) AS event_units
            FROM events
            WHERE event_id = ?
            """,
            (event_id,),
        ).fetchone()
        if row is None:
            raise InvalidRequest(f"unknown durable Event: {event_id}")
        try:
            event_units = int(row["event_units"])
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("Event storage size metadata is malformed") from exc
        if event_units <= 0:
            raise IntegrityViolation("Event storage size metadata is invalid")
        return event_units

    def _recall_materialization_lower_bound(
        self,
        program_id: str,
        source_ref: str,
    ) -> int:
        if source_ref.startswith(_EVIDENCE_REF_PREFIX):
            if self._evidence is None:
                raise InvalidRequest("Evidence recall requires the Host Evidence repository")
            evidence_id = source_ref[len(_EVIDENCE_REF_PREFIX) :]
            row = self._evidence._metadata_row(evidence_id)
            artifact_digest = str(row["artifact_digest"])
            byte_length = self._evidence._artifact_preflight(
                artifact_digest,
                expected_content_ref=self._evidence._content_ref(artifact_digest),
            )
            return (
                int(row["evidence_json_bytes"])
                + int(row["admission_json_bytes"])
                + 4 * ((byte_length + 2) // 3)
            )
        if source_ref.startswith(_CONTEXT_RECEIPT_PREFIX):
            row = self._host_store._db.execute(
                """
                SELECT
                    compiled_event_id,
                    length(CAST(context_json AS BLOB)) AS context_bytes,
                    length(CAST(receipt_json AS BLOB)) AS receipt_bytes,
                    used_units
                FROM context_receipts
                WHERE context_receipt_id = ?
                """,
                (source_ref,),
            ).fetchone()
            if row is None:
                raise InvalidRequest(f"unknown ContextReceipt: {source_ref}")
            try:
                context_bytes = int(row["context_bytes"])
                receipt_bytes = int(row["receipt_bytes"])
                used_units = int(row["used_units"])
            except (TypeError, ValueError) as exc:
                raise IntegrityViolation("ContextReceipt size metadata is malformed") from exc
            if context_bytes <= 0 or receipt_bytes <= 0 or used_units < 0:
                raise IntegrityViolation("ContextReceipt size metadata is invalid")
            return context_bytes + receipt_bytes + len(str(used_units))
        if source_ref.startswith(_EVENT_REF_PREFIX):
            event_id = source_ref[len(_EVENT_REF_PREFIX) :]
            persisted = self._host_store._db.execute(
                "SELECT event_id FROM context_persisted_source_index WHERE event_id = ?",
                (event_id,),
            ).fetchone()
            if persisted is not None:
                return self._persisted_source_preflight(
                    program_id,
                    source_ref,
                ).payload_units
            row = self._host_store._db.execute(
                """
                SELECT length(CAST(event_json AS BLOB)) AS event_bytes
                FROM events
                WHERE event_id = ?
                """,
                (event_id,),
            ).fetchone()
            if row is None:
                raise InvalidRequest(f"unknown durable Context address: {source_ref}")
            try:
                event_bytes = int(row["event_bytes"])
            except (TypeError, ValueError) as exc:
                raise IntegrityViolation("Event size metadata is malformed") from exc
            if event_bytes <= 0:
                raise IntegrityViolation("Event size metadata is invalid")
            return event_bytes
        return 0

    def _recall_storage_preflight(self, source_ref: str) -> bool:
        if source_ref.startswith(_EVIDENCE_REF_PREFIX):
            if self._evidence is None:
                raise InvalidRequest("Evidence recall requires the Host Evidence repository")
            evidence_id = source_ref[len(_EVIDENCE_REF_PREFIX) :]
            row = self._evidence._metadata_row(evidence_id)
            evidence_units = int(row["evidence_json_bytes"])
            admission_units = int(row["admission_json_bytes"])
            evidence_id = str(row["evidence_id"])
            if admission_units > _evidence_admission_storage_limit(evidence_id):
                return False
            event_units = self._event_storage_units(str(row["admitted_event_id"]))
            return event_units <= _evidence_event_storage_limit(
                evidence_id, evidence_units, admission_units
            )
        if source_ref.startswith(_CONTEXT_RECEIPT_PREFIX):
            row = self._host_store._db.execute(
                """
                SELECT compiled_event_id, program_id,
                       length(CAST(context_json AS BLOB)) AS context_bytes,
                       length(CAST(receipt_json AS BLOB)) AS receipt_bytes,
                       used_units
                FROM context_receipts
                WHERE context_receipt_id = ?
                """,
                (source_ref,),
            ).fetchone()
            if row is None:
                raise InvalidRequest(f"unknown ContextReceipt: {source_ref}")
            try:
                semantic_units = (
                    int(row["context_bytes"])
                    + int(row["receipt_bytes"])
                    + len(str(int(row["used_units"])))
                )
            except (TypeError, ValueError) as exc:
                raise IntegrityViolation("ContextReceipt size metadata is malformed") from exc
            event_units = self._event_storage_units(str(row["compiled_event_id"]))
            return event_units <= _compiled_context_event_storage_limit(
                str(row["program_id"]), semantic_units
            )
        return True

    def recall(
        self,
        program_id: str,
        source_refs: tuple[str, ...],
        *,
        max_items: int,
        max_units: int,
    ) -> RecallResult:
        program_row = self._host_store._db.execute(
            "SELECT program_id FROM program_projections WHERE program_id = ?",
            (program_id,),
        ).fetchone()
        if program_row is None or program_row["program_id"] != program_id:
            raise InvalidRequest(f"unknown Program: {program_id}")
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
            if not self._recall_storage_preflight(source_ref_value):
                excluded.append(source_ref_value)
                continue
            lower_bound = self._recall_materialization_lower_bound(
                program_id, source_ref_value
            )
            current_units = _canonical_units(
                {"sources": tuple(_source_entry(item) for item in items)}
            )
            if current_units + lower_bound > max_units:
                excluded.append(source_ref_value)
                continue
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

    def _current_evidence_metadata_preflight(
        self, evidence_id: str
    ) -> tuple[sqlite3.Row, int, int]:
        if self._evidence is None:
            raise InvalidRequest("current Evidence Context requires the Evidence repository")
        row = self._evidence._metadata_row(evidence_id)
        if row["currentness"] != "current":
            raise ContextIncomplete(
                f"Evidence is not current and cannot enter current-evidence Context: {evidence_id}"
            )
        artifact_digest = str(row["artifact_digest"])
        byte_length = self._evidence._artifact_preflight(
            artifact_digest,
            expected_content_ref=self._evidence._content_ref(artifact_digest),
        )

        event_units = self._contexts._event_storage_units(str(row["admitted_event_id"]))
        return row, byte_length, event_units

    def _validate_current_evidence_event_binding(self, row: sqlite3.Row) -> None:
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
            or indexed["indexed_evidence_id"] != row["evidence_id"]
            or indexed["indexed_event_type"] != "evidence.admitted"
            or indexed["semantic_event_id"] != row["admitted_event_id"]
            or indexed["semantic_event_type"] != "evidence.admitted"
        ):
            raise IntegrityViolation("Evidence preflight Event binding mismatch")

    def _current_evidence_preflight(self, evidence_id: str) -> tuple[Evidence, int]:
        if self._evidence is None:
            raise InvalidRequest("current Evidence Context requires the Evidence repository")
        metadata, byte_length, _event_units = self._current_evidence_metadata_preflight(
            evidence_id
        )
        self._validate_current_evidence_event_binding(metadata)
        row = self._evidence._row(evidence_id)
        if (
            row["evidence_id"] != metadata["evidence_id"]
            or row["artifact_digest"] != metadata["artifact_digest"]
            or row["admitted_event_id"] != metadata["admitted_event_id"]
            or row["evidence_record_digest"] != metadata["evidence_record_digest"]
            or row["admission_digest"] != metadata["admission_digest"]
        ):
            raise IntegrityViolation("Evidence metadata changed during preflight")
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
            or evidence.currentness != metadata["currentness"]
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
        if (
            artifact is None
            or int(artifact["byte_length"]) != byte_length
            or artifact["content_ref"] != evidence.content_ref
        ):
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

    def _capability_preflight(
        self,
        capability_snapshot: CapabilitySnapshot | None,
    ) -> _CapabilitySnapshotPreflight | None:
        if capability_snapshot is None:
            return None
        if self._capabilities is None:
            raise InvalidRequest(
                "Capability snapshot Context requires the Host Capability repository"
            )
        durable_digest, durable_units = self._capabilities._snapshot_metadata(
            capability_snapshot.snapshot_id
        )
        supplied_digest = canonical_digest(capability_snapshot)
        supplied_units = _canonical_units(capability_snapshot)
        if durable_digest != supplied_digest or durable_units != supplied_units:
            raise IntegrityViolation(
                "Capability snapshot metadata differs from supplied Context source"
            )
        return _CapabilitySnapshotPreflight(
            source_ref=f"{_CAPABILITY_REF_PREFIX}{capability_snapshot.snapshot_id}",
            snapshot_id=capability_snapshot.snapshot_id,
            snapshot_digest=durable_digest,
            payload_units=durable_units,
        )

    def _capability_context(
        self,
        capability_snapshot: CapabilitySnapshot | None,
        *,
        preflight: _CapabilitySnapshotPreflight | None = None,
    ) -> tuple[str | None, object | None]:
        if capability_snapshot is None:
            if preflight is not None:
                raise IntegrityViolation("Capability preflight exists without a snapshot")
            return None, None
        if self._capabilities is None:
            raise InvalidRequest(
                "Capability snapshot Context requires the Host Capability repository"
            )
        preflight = (
            self._capability_preflight(capability_snapshot)
            if preflight is None
            else preflight
        )
        if (
            preflight.snapshot_id != capability_snapshot.snapshot_id
            or preflight.source_ref
            != f"{_CAPABILITY_REF_PREFIX}{capability_snapshot.snapshot_id}"
        ):
            raise IntegrityViolation("Capability snapshot preflight identity mismatch")
        durable = self._capabilities.get_snapshot(capability_snapshot.snapshot_id)
        if durable != capability_snapshot:
            raise IntegrityViolation(
                "Capability snapshot differs from durable Host Context source"
            )
        payload = to_canonical_data(durable)
        if (
            canonical_digest(durable) != preflight.snapshot_digest
            or _canonical_units(payload) != preflight.payload_units
        ):
            raise IntegrityViolation(
                "Capability snapshot materialization diverges from size preflight"
            )
        return (preflight.source_ref, payload)

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

        program_preflight = self._contexts._current_program_preflight(program_id)
        empty_program_payload = freeze_json({})
        assert isinstance(empty_program_payload, FrozenMap)
        program_currentness, program_authority, program_historical = _PRIORITY_SEMANTICS[
            ContextPriority.CURRENT_PROGRAM
        ]
        program_shell = ContextSource(
            source_ref=program_preflight.source_ref,
            priority=ContextPriority.CURRENT_PROGRAM,
            payload=empty_program_payload,
            source_digest=program_preflight.projection_digest,
            currentness=program_currentness,
            authority=program_authority,
            historical=program_historical,
        )
        minimum_program_trial = self._build_context([program_shell], None)
        minimum_program_units = (
            _canonical_units(minimum_program_trial)
            - _canonical_units(empty_program_payload)
            + program_preflight.payload_units
        )
        evidence_source_refs = tuple(
            evidence_ref(evidence_id) for evidence_id in evidence_refs
        )
        required_host_control_refs = self._contexts._current_host_control_refs(
            program_id,
            program_preflight.program_revision,
            max_refs=max(0, budget_units // _MIN_HOST_CONTROL_SOURCE_UNITS),
        )
        effective_source_refs = tuple(sorted(set(source_refs) | set(required_host_control_refs)))
        requested_source_ids = (
            [program_preflight.source_ref]
            + list(effective_source_refs)
            + list(evidence_source_refs)
            + list(recalled_refs)
        )
        if len(set(requested_source_ids)) != len(requested_source_ids):
            raise InvalidRequest("Context compilation contains duplicate durable sources")

        persisted_preflights = tuple(
            self._contexts._persisted_source_preflight(program_id, source_ref_value)
            for source_ref_value in effective_source_refs
        )
        for preflight in persisted_preflights:
            if (
                preflight.priority is ContextPriority.HOST_CONTROL
                and preflight.program_revision != program_preflight.program_revision
            ):
                raise ContextIncomplete(
                    "Host control Context source is stale for current Program revision"
                )

        if recalled_refs:
            requested_recall_units = (
                budget_units if recall_max_units is None else recall_max_units
            )
            effective_recall_units = min(budget_units, requested_recall_units)
            if recall_max_items <= 0 or effective_recall_units <= 0:
                raise InvalidRequest(
                    "bounded recall requires positive item and size limits"
                )
            minimum_recall_units = _canonical_units({"sources": tuple()})
            if effective_recall_units < minimum_recall_units:
                raise ContextBudgetExceeded(
                    "bounded recall budget cannot fit the empty Context envelope"
                )
            for recalled_ref in sorted(recalled_refs):
                self._contexts._validate_recall_address(program_id, recalled_ref)

        if program_preflight.event_units > _program_event_storage_limit(
            program_preflight.program_id,
            program_preflight.payload_units,
        ):
            raise IntegrityViolation(
                "current Program Event storage exceeds bounded semantic envelope"
            )

        capability_preflight = self._capability_preflight(capability_snapshot)
        if capability_preflight is not None:
            empty_capability_payload = freeze_json({})
            assert isinstance(empty_capability_payload, FrozenMap)
            capability_shell_trial = self._build_context(
                [program_shell],
                empty_capability_payload,
            )
            minimum_program_units = (
                _canonical_units(capability_shell_trial)
                - _canonical_units(empty_program_payload)
                + program_preflight.payload_units
                - _canonical_units(empty_capability_payload)
                + capability_preflight.payload_units
            )

        if minimum_program_units > budget_units:
            raise ContextBudgetExceeded(
                "Context budget cannot fit mandatory Host control/current Program sources"
            )

        if capability_preflight is not None:
            assert capability_snapshot is not None
            self._capabilities._snapshot_binding_units(capability_snapshot.capabilities)

        capability_ref_value, capability_payload = self._capability_context(
            capability_snapshot,
            preflight=capability_preflight,
        )

        recall_result: RecallResult | None = None
        recalled_sources: list[ContextSource] = []
        if recalled_refs:
            recall_result = self._contexts.recall(
                program_id,
                recalled_refs,
                max_items=recall_max_items,
                max_units=effective_recall_units,
            )
            recalled_sources = self._sort_sources(list(recall_result.items))

        source_ids = (
            [program_preflight.source_ref]
            + [preflight.source_ref for preflight in persisted_preflights]
            + [source.source_ref for source in recalled_sources]
            + list(evidence_source_refs)
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

        program_shell_trial = self._build_context(
            [*included_sources, program_shell],
            capability_payload,
        )
        predicted_program_units = (
            _canonical_units(program_shell_trial)
            - _canonical_units(empty_program_payload)
            + program_preflight.payload_units
        )
        if predicted_program_units > budget_units:
            raise ContextBudgetExceeded(
                "Context budget cannot fit mandatory Host control/current Program sources"
            )
        program_source = self._contexts.current_program_source(
            program_id,
            preflight=program_preflight,
        )
        program_trial = self._build_context(
            [*included_sources, program_source],
            capability_payload,
        )
        if _canonical_units(program_trial) != predicted_program_units:
            raise IntegrityViolation(
                "current Program size preflight diverges from materialization"
            )
        included_sources.append(program_source)
        included_refs.append(program_source.source_ref)

        for evidence_id in sorted(evidence_refs):
            metadata, byte_length, event_units = (
                self._current_evidence_metadata_preflight(evidence_id)
            )
            source_ref_value = evidence_ref(evidence_id)
            encoded_length = 4 * ((byte_length + 2) // 3)
            current_units = _canonical_units(
                self._build_context(included_sources, capability_payload)
            )
            evidence_storage_units = int(metadata["evidence_json_bytes"])
            admission_storage_units = int(metadata["admission_json_bytes"])
            self._validate_current_evidence_event_binding(metadata)
            if admission_storage_units > _evidence_admission_storage_limit(evidence_id):
                excluded_refs.append(source_ref_value)
                continue
            if event_units > _evidence_event_storage_limit(
                evidence_id, evidence_storage_units, admission_storage_units
            ):
                excluded_refs.append(source_ref_value)
                continue
            empty_evidence = freeze_json({})
            assert isinstance(empty_evidence, FrozenMap)
            metadata_shell = _make_source(
                source_ref=source_ref_value,
                priority=ContextPriority.CURRENT_EVIDENCE,
                payload={"evidence": {}, "content_base64": ""},
            )
            metadata_shell_trial = self._build_context(
                [*included_sources, metadata_shell],
                capability_payload,
            )
            metadata_predicted_units = (
                _canonical_units(metadata_shell_trial)
                - _canonical_units(empty_evidence)
                + evidence_storage_units
                + encoded_length
            )
            if metadata_predicted_units > budget_units:
                excluded_refs.append(source_ref_value)
                continue
            evidence, verified_byte_length = self._current_evidence_preflight(evidence_id)
            if verified_byte_length != byte_length:
                raise IntegrityViolation("Evidence byte length changed during preflight")
            shell = _make_source(
                source_ref=source_ref_value,
                priority=ContextPriority.CURRENT_EVIDENCE,
                payload={
                    "evidence": to_canonical_data(evidence),
                    "content_base64": "",
                },
            )
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
            program_id=program_preflight.program_id,
            program_revision=program_preflight.program_revision,
            included_refs=tuple(included_refs),
            excluded_refs=tuple(excluded_refs),
            completeness=completeness,
            budget_units=budget_units,
            created_at=utc_now(),
        )
        return self._contexts._store_compilation(receipt, context, used_units)
