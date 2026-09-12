from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from ..kernel.durable_program import ProgramRepository
from ..kernel.errors import IntegrityViolation, InvalidRequest, PersistenceConflict
from ..kernel.events import utc_now
from ..kernel.schema_codec import record_from_json, record_to_json
from ..kernel.serialization import canonical_digest, to_canonical_data
from .workspace_blobs import WorkspaceBlobStore
from .workspace_capture import capture_workspace
from .workspace_schema import migrate_workspace_archive
from .workspace_types import (
    SNAPSHOT_PREFIX,
    WorkspaceArtifact,
    WorkspaceSnapshot,
    canonical_artifact_path,
    manifest_digest,
    require_text,
    validate_artifact,
    validate_snapshot,
)


class WorkspaceSnapshotStore:
    """Immutable workspace manifests bound to exact Program revisions."""

    def __init__(
        self,
        programs: ProgramRepository,
        *,
        workspace_root: str | Path,
        artifact_root: str | Path,
    ):
        self._programs = programs
        self.workspace_root = Path(workspace_root).resolve()
        self.artifact_root = Path(artifact_root).resolve()
        if self._overlaps(self.workspace_root, self.artifact_root):
            raise InvalidRequest("workspace and artifact roots must not overlap")
        migrate_workspace_archive(programs)
        self._blobs = WorkspaceBlobStore(programs, self.artifact_root)

    @staticmethod
    def _overlaps(left: Path, right: Path) -> bool:
        if left == right:
            return True
        try:
            left.relative_to(right)
            return True
        except ValueError:
            pass
        try:
            right.relative_to(left)
            return True
        except ValueError:
            return False

    def _decode_snapshot(self, row: sqlite3.Row) -> WorkspaceSnapshot:
        if type(row["program_revision"]) is not int:
            raise IntegrityViolation("workspace snapshot Program revision is malformed")
        try:
            snapshot = record_from_json(WorkspaceSnapshot, row["snapshot_json"])
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("workspace snapshot cannot be decoded") from exc
        if not isinstance(snapshot, WorkspaceSnapshot):
            raise IntegrityViolation("workspace snapshot decoded wrong type")
        validate_snapshot(snapshot)
        if (
            snapshot.snapshot_id != row["snapshot_id"]
            or snapshot.program_id != row["program_id"]
            or snapshot.program_revision != row["program_revision"]
            or snapshot.manifest_digest != row["manifest_digest"]
            or canonical_digest(snapshot) != row["snapshot_digest"]
        ):
            raise IntegrityViolation("workspace snapshot row/digest binding mismatch")
        return snapshot

    def _decode_entry(self, row: sqlite3.Row) -> WorkspaceArtifact:
        try:
            entry = record_from_json(WorkspaceArtifact, row["entry_json"])
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("workspace artifact entry cannot be decoded") from exc
        if not isinstance(entry, WorkspaceArtifact):
            raise IntegrityViolation("workspace artifact entry decoded wrong type")
        validate_artifact(entry)
        if (
            entry.path != row["path"]
            or entry.digest != row["artifact_digest"]
            or canonical_digest(entry) != row["entry_digest"]
        ):
            raise IntegrityViolation("workspace artifact entry row/digest binding mismatch")
        return entry

    def record(
        self,
        snapshot_id: str,
        *,
        authenticate_artifacts: bool = True,
    ) -> tuple[WorkspaceSnapshot, tuple[WorkspaceArtifact, ...]]:
        snapshot_id = require_text(snapshot_id, field="snapshot_id")
        row = self._programs._db.execute(
            """
            SELECT snapshot_id, program_id, program_revision, manifest_digest,
                   snapshot_json, snapshot_digest
            FROM workspace_snapshots WHERE snapshot_id = ?
            """,
            (snapshot_id,),
        ).fetchone()
        if row is None:
            raise InvalidRequest(f"unknown workspace snapshot: {snapshot_id}")
        snapshot = self._decode_snapshot(row)
        rows = self._programs._db.execute(
            """
            SELECT snapshot_id, path, artifact_digest, entry_json, entry_digest
            FROM workspace_snapshot_entries
            WHERE snapshot_id = ? ORDER BY path
            """,
            (snapshot_id,),
        ).fetchall()
        entries: list[WorkspaceArtifact] = []
        for entry_row in rows:
            if entry_row["snapshot_id"] != snapshot_id:
                raise IntegrityViolation("workspace artifact entry snapshot binding mismatch")
            entry = self._decode_entry(entry_row)
            if authenticate_artifacts:
                self._blobs.read(entry.digest, expected_length=entry.byte_length)
            entries.append(entry)
        result = tuple(entries)
        if len(result) != snapshot.entry_count:
            raise IntegrityViolation("workspace snapshot entry count mismatch")
        expected_manifest = manifest_digest(
            snapshot.program_id,
            snapshot.program_revision,
            result,
        )
        if expected_manifest != snapshot.manifest_digest:
            raise IntegrityViolation("workspace snapshot manifest digest mismatch")
        return snapshot, result

    @staticmethod
    def view(
        snapshot: WorkspaceSnapshot,
        entries: tuple[WorkspaceArtifact, ...],
    ) -> dict[str, Any]:
        return {
            "snapshot": to_canonical_data(snapshot),
            "artifacts": tuple(to_canonical_data(entry) for entry in entries),
        }

    def capture(self, program_id: str) -> dict[str, Any]:
        program_id = require_text(program_id, field="program_id")
        self._programs.verify_integrity(program_id)
        before = self._programs.get(program_id)
        captured = capture_workspace(self.workspace_root)
        self._programs.verify_integrity(program_id)
        if self._programs.get(program_id) != before:
            raise PersistenceConflict("Program changed during workspace snapshot capture")
        entries = tuple(entry for entry, _ in captured)
        digest = manifest_digest(before.program_id, before.revision, entries)
        snapshot_id = f"{SNAPSHOT_PREFIX}{digest}"
        exists = self._programs._db.execute(
            "SELECT 1 FROM workspace_snapshots WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
        if exists is not None:
            snapshot, stored = self.record(snapshot_id)
            if (
                snapshot.program_id != before.program_id
                or snapshot.program_revision != before.revision
                or stored != entries
            ):
                raise IntegrityViolation("workspace snapshot identity collision")
            return self.view(snapshot, stored)

        for entry, content in captured:
            self._blobs.store_file(content, entry.digest)
        snapshot = WorkspaceSnapshot(
            snapshot_id=snapshot_id,
            program_id=before.program_id,
            program_revision=before.revision,
            manifest_digest=digest,
            entry_count=len(entries),
            created_at=utc_now(),
        )
        validate_snapshot(snapshot)
        try:
            with self._programs._transaction():
                for entry in entries:
                    self._blobs.register_metadata(entry)
                self._programs._db.execute(
                    """
                    INSERT INTO workspace_snapshots(
                        snapshot_id, program_id, program_revision, manifest_digest,
                        snapshot_json, snapshot_digest
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot.snapshot_id,
                        snapshot.program_id,
                        snapshot.program_revision,
                        snapshot.manifest_digest,
                        record_to_json(snapshot),
                        canonical_digest(snapshot),
                    ),
                )
                for entry in entries:
                    self._programs._db.execute(
                        """
                        INSERT INTO workspace_snapshot_entries(
                            snapshot_id, path, artifact_digest, entry_json, entry_digest
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            snapshot.snapshot_id,
                            entry.path,
                            entry.digest,
                            record_to_json(entry),
                            canonical_digest(entry),
                        ),
                    )
        except sqlite3.IntegrityError as exc:
            raise PersistenceConflict("workspace snapshot identity collision") from exc
        return self.view(snapshot, entries)

    def list(self, program_id: str) -> tuple[dict[str, Any], ...]:
        program_id = require_text(program_id, field="program_id")
        self._programs.verify_integrity(program_id)
        rows = self._programs._db.execute(
            """
            SELECT snapshot_id FROM workspace_snapshots
            WHERE program_id = ? ORDER BY program_revision, snapshot_id
            """,
            (program_id,),
        ).fetchall()
        return tuple(self.view(*self.record(str(row["snapshot_id"]))) for row in rows)

    def show(self, snapshot_id: str) -> dict[str, Any]:
        return self.view(*self.record(snapshot_id))

    def artifacts(self, snapshot_id: str) -> tuple[dict[str, Any], ...]:
        _, entries = self.record(snapshot_id)
        return tuple(to_canonical_data(entry) for entry in entries)

    def artifact_bytes(self, snapshot_id: str, path: str) -> bytes:
        path = canonical_artifact_path(path)
        _, entries = self.record(snapshot_id, authenticate_artifacts=False)
        matches = tuple(entry for entry in entries if entry.path == path)
        if len(matches) != 1:
            raise InvalidRequest(f"unknown snapshot artifact: {path}")
        entry = matches[0]
        return self._blobs.read(entry.digest, expected_length=entry.byte_length)

    def store_imported_artifact(self, entry: WorkspaceArtifact, content: bytes) -> None:
        validate_artifact(entry)
        self._blobs.store_file(content, entry.digest)
        self._blobs.register_metadata(entry)

    def artifact_bytes_by_entry(self, entry: WorkspaceArtifact) -> bytes:
        validate_artifact(entry)
        return self._blobs.read(entry.digest, expected_length=entry.byte_length)
