from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from .durable_program import ProgramRepository
from .errors import EvidenceInvalid, EvidenceMissing, IntegrityViolation, PersistenceConflict
from .events import event_digest_fields, utc_now, verify_event_digest
from .models import Evidence, Event
from .schema_codec import record_from_json, record_to_json
from .serialization import canonical_digest, to_canonical_data


_COMPONENT = "evidence_store"
_COMPONENT_SCHEMA_VERSION = 4
_ARTIFACT_PREFIX = "evidence-artifact:"
_ARTIFACT_VERIFY_CHUNK_BYTES = 64 * 1024


def _evidence_metadata_projection_digest(
    *,
    evidence_id: str,
    artifact_digest: str,
    admitted_event_id: str,
    evidence_record_digest: str,
    admission_digest: str,
    currentness: str,
) -> str:
    return canonical_digest(
        {
            "projection": "evidence.context_metadata",
            "evidence_id": evidence_id,
            "artifact_digest": artifact_digest,
            "admitted_event_id": admitted_event_id,
            "evidence_record_digest": evidence_record_digest,
            "admission_digest": admission_digest,
            "currentness": currentness,
        }
    )


@dataclass(frozen=True, slots=True)
class EvidenceAdmissionReceipt:
    admission_id: str
    evidence_id: str
    artifact_digest: str
    admitted_at: str


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    evidence_id: str
    source_class: str
    observed_at: str
    digest: str
    trust_class: str
    currentness: str
    provenance: tuple[str, ...]


class EvidenceRepository:
    """Host-owned explicit Evidence admission over content-addressed source bytes."""

    def __init__(
        self,
        host_store: ProgramRepository,
        artifact_root: str | Path | None = None,
    ):
        self._host_store = host_store
        if artifact_root is None:
            database_path = str(host_store._database_path)
            if database_path == ":memory:":
                raise EvidenceInvalid(
                    "in-memory Host stores require an explicit Evidence artifact root"
                )
            artifact_root = Path(database_path).resolve().parent / "evidence"
        self._artifact_root = Path(artifact_root)
        self._mkdir_durable(self._artifact_root)
        self._migrate()

    @staticmethod
    def _windows_fsync_directory(path: Path) -> None:
        import ctypes
        from ctypes import wintypes

        generic_write = 0x40000000
        file_share_read = 0x00000001
        file_share_write = 0x00000002
        file_share_delete = 0x00000004
        open_existing = 3
        file_flag_backup_semantics = 0x02000000

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        flush_file_buffers = kernel32.FlushFileBuffers
        flush_file_buffers.argtypes = (wintypes.HANDLE,)
        flush_file_buffers.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL

        handle = create_file(
            str(path),
            generic_write,
            file_share_read | file_share_write | file_share_delete,
            None,
            open_existing,
            file_flag_backup_semantics,
            None,
        )
        invalid_handle = wintypes.HANDLE(-1).value
        if handle == invalid_handle:
            error = ctypes.get_last_error()
            raise PersistenceConflict(
                f"cannot open Evidence artifact directory for durable flush: {path}"
            ) from ctypes.WinError(error)
        try:
            if not flush_file_buffers(handle):
                error = ctypes.get_last_error()
                raise PersistenceConflict(
                    f"cannot durably flush Evidence artifact directory: {path}"
                ) from ctypes.WinError(error)
        finally:
            close_handle(handle)

    @staticmethod
    def _windows_replace_durable(source: Path, destination: Path) -> None:
        import ctypes
        from ctypes import wintypes

        movefile_replace_existing = 0x00000001
        movefile_write_through = 0x00000008
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        move_file_ex = kernel32.MoveFileExW
        move_file_ex.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
        move_file_ex.restype = wintypes.BOOL
        if not move_file_ex(
            str(source),
            str(destination),
            movefile_replace_existing | movefile_write_through,
        ):
            error = ctypes.get_last_error()
            raise PersistenceConflict(
                f"cannot durably replace Evidence artifact: {destination}"
            ) from ctypes.WinError(error)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":
            EvidenceRepository._windows_fsync_directory(path)
            return
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise PersistenceConflict(
                f"cannot open Evidence artifact directory for durable flush: {path}"
            ) from exc
        try:
            os.fsync(descriptor)
        except OSError as exc:
            raise PersistenceConflict(
                f"cannot durably flush Evidence artifact directory: {path}"
            ) from exc
        finally:
            os.close(descriptor)

    def _replace_durable(self, source: Path, destination: Path) -> None:
        if os.name == "nt":
            self._windows_replace_durable(source, destination)
        else:
            os.replace(source, destination)
        self._fsync_directory(destination.parent)

    def _mkdir_durable(self, path: Path) -> None:
        """Create a directory chain and durably persist every new directory entry."""
        missing: list[Path] = []
        current = path
        while not current.exists():
            missing.append(current)
            parent = current.parent
            if parent == current:
                break
            current = parent
        path.mkdir(parents=True, exist_ok=True)
        for created in reversed(missing):
            self._fsync_directory(created.parent)

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
                    f"Evidence schema version {version} is newer than supported "
                    f"{_COMPONENT_SCHEMA_VERSION}"
                )
            if version not in {None, 1, 2, 3, _COMPONENT_SCHEMA_VERSION}:
                raise IntegrityViolation(f"unsupported Evidence schema version {version}")

            if version is None:
                self._host_store._db.execute(
                    """
                    CREATE TABLE evidence_artifacts (
                        artifact_digest TEXT PRIMARY KEY,
                        content_ref TEXT NOT NULL UNIQUE,
                        byte_length INTEGER NOT NULL
                    )
                    """
                )
                self._host_store._db.execute(
                    """
                    CREATE TABLE evidence_records (
                        evidence_id TEXT PRIMARY KEY,
                        artifact_digest TEXT NOT NULL,
                        admitted_event_id TEXT NOT NULL UNIQUE,
                        evidence_json TEXT NOT NULL,
                        evidence_record_digest TEXT NOT NULL,
                        admission_json TEXT NOT NULL,
                        admission_digest TEXT NOT NULL,
                        currentness TEXT NOT NULL,
                        metadata_projection_digest TEXT NOT NULL,
                        FOREIGN KEY(artifact_digest) REFERENCES evidence_artifacts(artifact_digest)
                    )
                    """
                )

            if version in {None, 1}:
                self._host_store._db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS evidence_event_index (
                        sequence INTEGER PRIMARY KEY,
                        evidence_id TEXT NOT NULL,
                        event_id TEXT NOT NULL UNIQUE,
                        event_type TEXT NOT NULL,
                        FOREIGN KEY(sequence) REFERENCES events(sequence)
                    )
                    """
                )
                self._host_store._db.execute(
                    """
                    CREATE INDEX IF NOT EXISTS evidence_event_index_identity_sequence
                    ON evidence_event_index(evidence_id, sequence)
                    """
                )

            if version in {1, 2}:
                columns = {
                    str(column["name"])
                    for column in self._host_store._db.execute(
                        "PRAGMA table_info(evidence_records)"
                    ).fetchall()
                }
                if "currentness" not in columns:
                    self._host_store._db.execute(
                        "ALTER TABLE evidence_records ADD COLUMN currentness TEXT"
                    )
                if "metadata_projection_digest" not in columns:
                    self._host_store._db.execute(
                        "ALTER TABLE evidence_records ADD COLUMN metadata_projection_digest TEXT"
                    )

            if version in {None, 1, 2, 3}:
                self._rebuild_event_index()
            if version in {1, 2}:
                self._rebuild_metadata_projection()

            self._install_event_integrity_guard()

            if version is None:
                self._host_store._db.execute(
                    "INSERT INTO component_schema(component, version) VALUES (?, ?)",
                    (_COMPONENT, _COMPONENT_SCHEMA_VERSION),
                )
            elif version in {1, 2, 3}:
                self._host_store._db.execute(
                    "UPDATE component_schema SET version = ? WHERE component = ?",
                    (_COMPONENT_SCHEMA_VERSION, _COMPONENT),
                )

    def _install_event_integrity_guard(self) -> None:
        self._host_store._db.execute(
            "DROP TRIGGER IF EXISTS evidence_admission_event_integrity_invalidate"
        )
        self._host_store._db.execute(
            """
            CREATE TRIGGER evidence_admission_event_integrity_invalidate
            AFTER UPDATE OF event_json, event_digest, event_type, program_id ON events
            WHEN EXISTS (
                SELECT 1 FROM evidence_event_index
                WHERE event_id = OLD.event_id
            ) AND (
                OLD.event_json IS NOT NEW.event_json
                OR OLD.event_digest IS NOT NEW.event_digest
                OR OLD.event_type IS NOT NEW.event_type
                OR OLD.program_id IS NOT NEW.program_id
            )
            BEGIN
                DELETE FROM evidence_event_index WHERE event_id = OLD.event_id;
            END
            """
        )

    def _decode_event_row(self, row: sqlite3.Row) -> Event:
        try:
            event = record_from_json(Event, row["event_json"])
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("Evidence Event cannot be decoded") from exc
        if not isinstance(event, Event):
            raise IntegrityViolation("Evidence Event decoded wrong type")
        if (
            event.sequence != int(row["sequence"])
            or event.event_id != row["event_id"]
            or event.program_id != row["program_id"]
            or event.event_type != row["event_type"]
            or event.digest != row["event_digest"]
            or not verify_event_digest(event)
        ):
            raise IntegrityViolation("Evidence Event integrity mismatch")
        return event

    def _validate_rebuilt_event_record_binding(self, event: Event) -> None:
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
        if event.program_id is not None:
            raise IntegrityViolation(
                "Evidence admission Event must remain Host-scoped during migration"
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
        rows = self._host_store._db.execute(
            """
            SELECT sequence, event_id, program_id, event_type, event_json, event_digest
            FROM events
            WHERE event_type = 'evidence.admitted'
            ORDER BY sequence
            """
        )
        for row in rows:
            event = self._decode_event_row(row)
            if event.event_type != "evidence.admitted":
                raise IntegrityViolation("Evidence Event type projection mismatch")
            if not event.correlation_id:
                raise IntegrityViolation("Evidence admission Event lacks Evidence identity")
            self._validate_rebuilt_event_record_binding(event)
            self._host_store._db.execute(
                """
                INSERT INTO evidence_event_index(sequence, evidence_id, event_id, event_type)
                VALUES (?, ?, ?, ?)
                """,
                (event.sequence, event.correlation_id, event.event_id, event.event_type),
            )

        record_rows = self._host_store._db.execute(
            "SELECT evidence_id, admitted_event_id FROM evidence_records"
        )
        for record in record_rows:
            indexed = self._host_store._db.execute(
                """
                SELECT sequence, evidence_id, event_id, event_type
                FROM evidence_event_index WHERE event_id = ?
                """,
                (record["admitted_event_id"],),
            ).fetchone()
            if (
                indexed is None
                or indexed["evidence_id"] != record["evidence_id"]
                or indexed["event_type"] != "evidence.admitted"
            ):
                raise IntegrityViolation(
                    "Evidence record is not represented by the rebuilt Event index"
                )

    def _rebuild_metadata_projection(self) -> None:
        last_evidence_id = ""
        while True:
            row = self._host_store._db.execute(
                """
                SELECT evidence_id, artifact_digest, admitted_event_id,
                       evidence_json, evidence_record_digest,
                       admission_json, admission_digest
                FROM evidence_records
                WHERE evidence_id > ?
                ORDER BY evidence_id
                LIMIT 1
                """,
                (last_evidence_id,),
            ).fetchone()
            if row is None:
                break
            last_evidence_id = str(row["evidence_id"])
            try:
                evidence = record_from_json(Evidence, row["evidence_json"])
                admission = record_from_json(
                    EvidenceAdmissionReceipt,
                    row["admission_json"],
                )
            except (TypeError, ValueError) as exc:
                raise IntegrityViolation(
                    "Evidence metadata projection source cannot be decoded"
                ) from exc
            if not isinstance(evidence, Evidence) or not isinstance(
                admission, EvidenceAdmissionReceipt
            ):
                raise IntegrityViolation("Evidence metadata projection decoded wrong type")
            if (
                evidence.evidence_id != row["evidence_id"]
                or evidence.digest != row["artifact_digest"]
                or canonical_digest(evidence) != row["evidence_record_digest"]
                or canonical_digest(admission) != row["admission_digest"]
                or admission.evidence_id != evidence.evidence_id
                or admission.artifact_digest != evidence.digest
            ):
                raise IntegrityViolation("Evidence metadata projection source mismatch")
            self._validate_evidence(evidence)
            projection_digest = _evidence_metadata_projection_digest(
                evidence_id=evidence.evidence_id,
                artifact_digest=evidence.digest,
                admitted_event_id=str(row["admitted_event_id"]),
                evidence_record_digest=str(row["evidence_record_digest"]),
                admission_digest=str(row["admission_digest"]),
                currentness=evidence.currentness,
            )
            self._host_store._db.execute(
                """
                UPDATE evidence_records
                SET currentness = ?, metadata_projection_digest = ?
                WHERE evidence_id = ?
                """,
                (evidence.currentness, projection_digest, evidence.evidence_id),
            )

    @staticmethod
    def _parse_time(value: str, *, field: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise EvidenceInvalid(f"{field} must be valid ISO-8601") from exc
        if parsed.tzinfo is None:
            raise EvidenceInvalid(f"{field} must be timezone-aware")
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _artifact_digest(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    @staticmethod
    def _content_ref(artifact_digest: str) -> str:
        return f"{_ARTIFACT_PREFIX}{artifact_digest}"

    def _artifact_path(self, artifact_digest: str) -> Path:
        if len(artifact_digest) != 64:
            raise EvidenceInvalid("Evidence artifact digest must be a SHA-256 hex digest")
        try:
            int(artifact_digest, 16)
        except ValueError as exc:
            raise EvidenceInvalid("Evidence artifact digest must be hexadecimal") from exc
        return self._artifact_root / artifact_digest[:2] / artifact_digest[2:]

    def _store_artifact(self, content: bytes, artifact_digest: str) -> None:
        path = self._artifact_path(artifact_digest)
        self._mkdir_durable(path.parent)
        if path.exists():
            existing = path.read_bytes()
            if existing != content or self._artifact_digest(existing) != artifact_digest:
                raise IntegrityViolation("content-addressed Evidence artifact collision")
            self._fsync_directory(path.parent)
            return

        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with open(temporary, "xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            self._replace_durable(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def _artifact_stored_length(self, artifact_digest: str) -> int:
        path = self._artifact_path(artifact_digest)
        try:
            stored_length = path.stat().st_size
        except FileNotFoundError as exc:
            raise IntegrityViolation("Evidence artifact is missing") from exc
        except OSError as exc:
            raise IntegrityViolation("Evidence artifact metadata cannot be read") from exc
        if stored_length <= 0:
            raise IntegrityViolation("Evidence artifact byte length is invalid")
        return stored_length

    def _authenticate_artifact_content(
        self,
        artifact_digest: str,
        *,
        expected_length: int,
    ) -> None:
        path = self._artifact_path(artifact_digest)
        remaining = expected_length
        digest = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                while remaining > 0:
                    chunk = handle.read(min(_ARTIFACT_VERIFY_CHUNK_BYTES, remaining))
                    if not chunk:
                        raise IntegrityViolation("Evidence artifact byte length mismatch")
                    digest.update(chunk)
                    remaining -= len(chunk)
                if handle.read(1):
                    raise IntegrityViolation("Evidence artifact byte length mismatch")
        except OSError as exc:
            raise IntegrityViolation("Evidence artifact cannot be read") from exc
        if digest.hexdigest() != artifact_digest:
            raise IntegrityViolation("Evidence artifact digest mismatch")

    def _artifact_preflight(
        self,
        artifact_digest: str,
        *,
        expected_content_ref: str | None = None,
    ) -> int:
        row = self._host_store._db.execute(
            """
            SELECT content_ref, byte_length FROM evidence_artifacts
            WHERE artifact_digest = ?
            """,
            (artifact_digest,),
        ).fetchone()
        if row is None:
            raise IntegrityViolation("Evidence artifact metadata is missing")
        try:
            expected_length = int(row["byte_length"])
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("Evidence artifact byte length is malformed") from exc
        if expected_length <= 0:
            raise IntegrityViolation("Evidence artifact byte length is invalid")
        if (
            expected_content_ref is not None
            and row["content_ref"] != expected_content_ref
        ):
            raise IntegrityViolation("Evidence artifact content reference mismatch")
        if self._artifact_stored_length(artifact_digest) != expected_length:
            raise IntegrityViolation("Evidence artifact byte length mismatch")
        self._authenticate_artifact_content(
            artifact_digest,
            expected_length=expected_length,
        )
        return expected_length

    def _read_artifact(self, artifact_digest: str, *, expected_length: int) -> bytes:
        if expected_length <= 0:
            raise IntegrityViolation("Evidence artifact byte length is invalid")
        path = self._artifact_path(artifact_digest)
        if self._artifact_stored_length(artifact_digest) != expected_length:
            raise IntegrityViolation("Evidence artifact byte length mismatch")
        try:
            with path.open("rb") as handle:
                content = handle.read(expected_length + 1)
        except OSError as exc:
            raise IntegrityViolation("Evidence artifact cannot be read") from exc
        if len(content) != expected_length:
            raise IntegrityViolation("Evidence artifact byte length mismatch")
        if self._artifact_digest(content) != artifact_digest:
            raise IntegrityViolation("Evidence artifact digest mismatch")
        return content

    def _append_event(self, payload: object, *, evidence_id: str) -> Event:
        sequence = self._host_store._next_sequence()
        event_id = str(uuid4())
        occurred_at = utc_now()
        recorded_at = utc_now()
        canonical_payload = to_canonical_data(payload)
        digest = event_digest_fields(
            event_id=event_id,
            sequence=sequence,
            event_type="evidence.admitted",
            occurred_at=occurred_at,
            recorded_at=recorded_at,
            payload=canonical_payload,
            actor_id=None,
            program_id=None,
            causation_id=None,
            correlation_id=evidence_id,
        )
        event = Event(
            event_id=event_id,
            sequence=sequence,
            event_type="evidence.admitted",
            occurred_at=occurred_at,
            recorded_at=recorded_at,
            payload=canonical_payload,
            digest=digest,
            correlation_id=evidence_id,
        )
        self._host_store._insert_event(event)
        self._host_store._db.execute(
            """
            INSERT INTO evidence_event_index(sequence, evidence_id, event_id, event_type)
            VALUES (?, ?, ?, ?)
            """,
            (event.sequence, evidence_id, event.event_id, event.event_type),
        )
        return event

    def _event(self, event_id: str) -> Event:
        row = self._host_store._db.execute(
            """
            SELECT sequence, event_id, program_id, event_type, event_json, event_digest
            FROM events WHERE event_id = ?
            """,
            (event_id,),
        ).fetchone()
        if row is None:
            raise IntegrityViolation("Evidence admission Event is missing")
        event = self._decode_event_row(row)
        if event.event_type != "evidence.admitted":
            raise IntegrityViolation("Evidence record is anchored to the wrong Event type")
        return event

    def _indexed_events(self, evidence_id: str) -> tuple[Event, ...]:
        rows = self._host_store._db.execute(
            """
            SELECT sequence, evidence_id, event_id, event_type
            FROM evidence_event_index
            WHERE evidence_id = ?
            ORDER BY sequence
            """,
            (evidence_id,),
        ).fetchall()
        events: list[Event] = []
        for row in rows:
            event = self._event(str(row["event_id"]))
            if (
                event.sequence != int(row["sequence"])
                or event.correlation_id != row["evidence_id"]
                or event.event_type != row["event_type"]
                or row["evidence_id"] != evidence_id
            ):
                raise IntegrityViolation("Evidence Event index diverges from semantic Event")
            events.append(event)
        return tuple(events)

    def _identity_exists_in_history(self, evidence_id: str) -> bool:
        return bool(self._indexed_events(evidence_id))

    @classmethod
    def _validate_evidence(cls, evidence: Evidence) -> None:
        if not evidence.evidence_id.strip() or not evidence.source_class.strip():
            raise EvidenceInvalid("Evidence identity and source class must be non-empty")
        if evidence.content_ref != cls._content_ref(evidence.digest):
            raise EvidenceInvalid("Evidence content reference disagrees with content digest")
        if len(evidence.digest) != 64:
            raise EvidenceInvalid("Evidence digest must be a SHA-256 hex digest")
        try:
            int(evidence.digest, 16)
        except ValueError as exc:
            raise EvidenceInvalid("Evidence digest must be hexadecimal") from exc
        if (
            type(evidence.provenance) is not tuple
            or not evidence.provenance
            or any(type(item) is not str or not item.strip() for item in evidence.provenance)
        ):
            raise EvidenceInvalid("Evidence requires a tuple of non-empty provenance strings")
        if not evidence.trust_class.strip() or not evidence.currentness.strip():
            raise EvidenceInvalid("Evidence trust/currentness metadata must be non-empty")
        cls._parse_time(evidence.observed_at, field="observed_at")

    def admit(
        self,
        *,
        content: bytes,
        source_class: str,
        observed_at: str,
        provenance: tuple[str, ...],
        trust_class: str,
        currentness: str,
        evidence_id: str | None = None,
    ) -> Evidence:
        if type(content) is not bytes:
            raise EvidenceInvalid("Evidence admission requires exact source bytes")
        if not content:
            raise EvidenceInvalid("Evidence content must be non-empty")
        artifact_digest = self._artifact_digest(content)
        evidence = Evidence(
            evidence_id=str(uuid4()) if evidence_id is None else evidence_id,
            source_class=source_class,
            observed_at=observed_at,
            content_ref=self._content_ref(artifact_digest),
            digest=artifact_digest,
            provenance=provenance,
            trust_class=trust_class,
            currentness=currentness,
        )
        self._validate_evidence(evidence)
        if self._identity_exists_in_history(evidence.evidence_id):
            raise PersistenceConflict(
                f"Evidence identity already exists in durable history: {evidence.evidence_id}"
            )
        admission = EvidenceAdmissionReceipt(
            admission_id=str(uuid4()),
            evidence_id=evidence.evidence_id,
            artifact_digest=artifact_digest,
            admitted_at=utc_now(),
        )
        self._store_artifact(content, artifact_digest)
        try:
            with self._host_store._transaction():
                if self._identity_exists_in_history(evidence.evidence_id):
                    raise PersistenceConflict(
                        f"Evidence identity already exists in durable history: {evidence.evidence_id}"
                    )
                self._host_store._db.execute(
                    """
                    INSERT INTO evidence_artifacts(artifact_digest, content_ref, byte_length)
                    VALUES (?, ?, ?)
                    ON CONFLICT(artifact_digest) DO NOTHING
                    """,
                    (artifact_digest, evidence.content_ref, len(content)),
                )
                artifact_row = self._host_store._db.execute(
                    """
                    SELECT content_ref, byte_length FROM evidence_artifacts
                    WHERE artifact_digest = ?
                    """,
                    (artifact_digest,),
                ).fetchone()
                if (
                    artifact_row is None
                    or artifact_row["content_ref"] != evidence.content_ref
                    or int(artifact_row["byte_length"]) != len(content)
                ):
                    raise IntegrityViolation("durable Evidence artifact metadata collision")
                event = self._append_event(
                    {"evidence": evidence, "admission": admission},
                    evidence_id=evidence.evidence_id,
                )
                evidence_json = record_to_json(evidence)
                evidence_record_digest = canonical_digest(evidence)
                admission_json = record_to_json(admission)
                admission_digest = canonical_digest(admission)
                metadata_projection_digest = _evidence_metadata_projection_digest(
                    evidence_id=evidence.evidence_id,
                    artifact_digest=artifact_digest,
                    admitted_event_id=event.event_id,
                    evidence_record_digest=evidence_record_digest,
                    admission_digest=admission_digest,
                    currentness=evidence.currentness,
                )
                self._host_store._db.execute(
                    """
                    INSERT INTO evidence_records(
                        evidence_id, artifact_digest, admitted_event_id,
                        evidence_json, evidence_record_digest,
                        admission_json, admission_digest, currentness,
                        metadata_projection_digest
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        evidence.evidence_id,
                        artifact_digest,
                        event.event_id,
                        evidence_json,
                        evidence_record_digest,
                        admission_json,
                        admission_digest,
                        evidence.currentness,
                        metadata_projection_digest,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise PersistenceConflict(
                f"Evidence identity already exists: {evidence.evidence_id}"
            ) from exc
        return evidence

    def _metadata_row(self, evidence_id: str) -> sqlite3.Row:
        row = self._host_store._db.execute(
            """
            SELECT evidence_id, artifact_digest, admitted_event_id,
                   evidence_record_digest, admission_digest, currentness,
                   metadata_projection_digest,
                   length(CAST(evidence_json AS BLOB)) AS evidence_json_bytes,
                   length(CAST(admission_json AS BLOB)) AS admission_json_bytes
            FROM evidence_records WHERE evidence_id = ?
            """,
            (evidence_id,),
        ).fetchone()
        if row is None:
            raise EvidenceMissing(f"unknown Evidence: {evidence_id}")
        try:
            evidence_json_bytes = int(row["evidence_json_bytes"])
            admission_json_bytes = int(row["admission_json_bytes"])
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("Evidence metadata lengths are malformed") from exc
        currentness = row["currentness"]
        projection_digest = row["metadata_projection_digest"]
        if (
            evidence_json_bytes <= 0
            or admission_json_bytes <= 0
            or type(currentness) is not str
            or not currentness.strip()
            or type(projection_digest) is not str
            or not projection_digest.strip()
        ):
            raise IntegrityViolation("Evidence metadata projection is invalid")
        expected_projection_digest = _evidence_metadata_projection_digest(
            evidence_id=str(row["evidence_id"]),
            artifact_digest=str(row["artifact_digest"]),
            admitted_event_id=str(row["admitted_event_id"]),
            evidence_record_digest=str(row["evidence_record_digest"]),
            admission_digest=str(row["admission_digest"]),
            currentness=currentness,
        )
        if projection_digest != expected_projection_digest:
            raise IntegrityViolation("Evidence metadata projection digest mismatch")
        return row

    def _row(self, evidence_id: str) -> sqlite3.Row:
        row = self._host_store._db.execute(
            """
            SELECT evidence_id, artifact_digest, admitted_event_id,
                   evidence_json, evidence_record_digest,
                   admission_json, admission_digest
            FROM evidence_records WHERE evidence_id = ?
            """,
            (evidence_id,),
        ).fetchone()
        if row is None:
            raise EvidenceMissing(f"unknown Evidence: {evidence_id}")
        return row

    def get(self, evidence_id: str) -> Evidence:
        row = self._row(evidence_id)
        try:
            evidence = record_from_json(Evidence, row["evidence_json"])
            admission = record_from_json(EvidenceAdmissionReceipt, row["admission_json"])
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("Evidence record cannot be decoded") from exc
        if not isinstance(evidence, Evidence) or not isinstance(
            admission, EvidenceAdmissionReceipt
        ):
            raise IntegrityViolation("Evidence record decoded wrong type")
        if evidence.evidence_id != row["evidence_id"]:
            raise IntegrityViolation("Evidence row identity mismatch")
        if evidence.digest != row["artifact_digest"]:
            raise IntegrityViolation("Evidence artifact digest binding mismatch")
        if canonical_digest(evidence) != row["evidence_record_digest"]:
            raise IntegrityViolation("Evidence record digest mismatch")
        if canonical_digest(admission) != row["admission_digest"]:
            raise IntegrityViolation("Evidence admission receipt digest mismatch")
        if (
            admission.evidence_id != evidence.evidence_id
            or admission.artifact_digest != evidence.digest
        ):
            raise IntegrityViolation("Evidence admission receipt binding mismatch")
        self._validate_evidence(evidence)

        artifact = self._host_store._db.execute(
            """
            SELECT content_ref, byte_length FROM evidence_artifacts
            WHERE artifact_digest = ?
            """,
            (evidence.digest,),
        ).fetchone()
        if artifact is None:
            raise IntegrityViolation("Evidence artifact metadata is missing")
        if artifact["content_ref"] != evidence.content_ref:
            raise IntegrityViolation("Evidence artifact content reference mismatch")
        self._read_artifact(
            evidence.digest,
            expected_length=int(artifact["byte_length"]),
        )

        indexed = self._host_store._db.execute(
            """
            SELECT sequence, evidence_id, event_id, event_type
            FROM evidence_event_index WHERE event_id = ?
            """,
            (row["admitted_event_id"],),
        ).fetchone()
        if indexed is None:
            raise IntegrityViolation("Evidence admission Event index entry is missing")
        event = self._event(str(row["admitted_event_id"]))
        if (
            event.sequence != int(indexed["sequence"])
            or indexed["evidence_id"] != evidence.evidence_id
            or indexed["event_id"] != event.event_id
            or indexed["event_type"] != event.event_type
            or event.correlation_id != evidence.evidence_id
        ):
            raise IntegrityViolation("Evidence admission Event index binding mismatch")
        expected_payload = to_canonical_data(
            {"evidence": evidence, "admission": admission}
        )
        if to_canonical_data(event.payload) != expected_payload:
            raise IntegrityViolation("Evidence record diverges from admission Event")
        return evidence

    def admission(self, evidence_id: str) -> EvidenceAdmissionReceipt:
        row = self._row(evidence_id)
        self.get(evidence_id)
        try:
            admission = record_from_json(
                EvidenceAdmissionReceipt,
                row["admission_json"],
            )
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("Evidence admission receipt cannot be decoded") from exc
        assert isinstance(admission, EvidenceAdmissionReceipt)
        return admission

    def artifact(self, evidence_id: str) -> bytes:
        evidence = self.get(evidence_id)
        row = self._host_store._db.execute(
            """
            SELECT byte_length FROM evidence_artifacts WHERE artifact_digest = ?
            """,
            (evidence.digest,),
        ).fetchone()
        if row is None:
            raise IntegrityViolation("Evidence artifact metadata is missing")
        return self._read_artifact(
            evidence.digest,
            expected_length=int(row["byte_length"]),
        )

    def reference(self, evidence_id: str) -> EvidenceReference:
        evidence = self.get(evidence_id)
        return EvidenceReference(
            evidence_id=evidence.evidence_id,
            source_class=evidence.source_class,
            observed_at=evidence.observed_at,
            digest=evidence.digest,
            trust_class=evidence.trust_class,
            currentness=evidence.currentness,
            provenance=evidence.provenance,
        )
