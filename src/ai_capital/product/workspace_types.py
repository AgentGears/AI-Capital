from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath

from ..kernel.errors import IntegrityViolation, InvalidRequest
from ..kernel.serialization import canonical_digest


SNAPSHOT_PREFIX = "workspace-snapshot:"
ARTIFACT_PREFIX = "workspace-artifact:"
BUNDLE_PREFIX = "program-bundle:"


@dataclass(frozen=True, slots=True)
class WorkspaceArtifact:
    path: str
    digest: str
    byte_length: int
    content_ref: str


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    snapshot_id: str
    program_id: str
    program_revision: int
    manifest_digest: str
    entry_count: int
    created_at: str


def require_text(value: str, *, field: str) -> str:
    if type(value) is not str or not value.strip():
        raise InvalidRequest(f"{field} must be non-empty")
    return value


def validate_digest(value: str, *, field: str) -> None:
    require_text(value, field=field)
    if len(value) != 64:
        raise InvalidRequest(f"{field} must be a SHA-256 hex digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise InvalidRequest(f"{field} must be hexadecimal") from exc


def validate_timestamp(value: str, *, field: str) -> None:
    require_text(value, field=field)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidRequest(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise InvalidRequest(f"{field} must be timezone-aware")


def canonical_artifact_path(value: str) -> str:
    value = require_text(value, field="artifact path")
    if "\\" in value:
        raise InvalidRequest("artifact path must use canonical POSIX separators")
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or not candidate.parts:
        raise InvalidRequest("artifact path must be relative")
    if any(part in {"", ".", ".."} for part in candidate.parts):
        raise InvalidRequest("artifact path cannot contain traversal segments")
    canonical = candidate.as_posix()
    if canonical != value:
        raise InvalidRequest("artifact path is not canonical")
    return canonical


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def validate_artifact(entry: WorkspaceArtifact) -> None:
    canonical_artifact_path(entry.path)
    try:
        validate_digest(entry.digest, field="artifact digest")
    except InvalidRequest as exc:
        raise IntegrityViolation("workspace artifact digest is invalid") from exc
    if type(entry.byte_length) is not int or entry.byte_length < 0:
        raise IntegrityViolation("workspace artifact byte length is invalid")
    if entry.content_ref != f"{ARTIFACT_PREFIX}{entry.digest}":
        raise IntegrityViolation("workspace artifact content reference mismatch")


def validate_snapshot(snapshot: WorkspaceSnapshot) -> None:
    require_text(snapshot.program_id, field="program_id")
    if type(snapshot.program_revision) is not int or snapshot.program_revision < 0:
        raise IntegrityViolation("workspace snapshot Program revision is invalid")
    try:
        validate_digest(snapshot.manifest_digest, field="manifest digest")
        validate_timestamp(snapshot.created_at, field="snapshot created_at")
    except InvalidRequest as exc:
        raise IntegrityViolation("workspace snapshot metadata is invalid") from exc
    if snapshot.snapshot_id != f"{SNAPSHOT_PREFIX}{snapshot.manifest_digest}":
        raise IntegrityViolation("workspace snapshot identity mismatch")
    if type(snapshot.entry_count) is not int or snapshot.entry_count < 0:
        raise IntegrityViolation("workspace snapshot entry count is invalid")


def manifest_digest(
    program_id: str,
    program_revision: int,
    entries: tuple[WorkspaceArtifact, ...],
) -> str:
    return canonical_digest(
        {
            "program_id": program_id,
            "program_revision": program_revision,
            "artifacts": entries,
        }
    )
