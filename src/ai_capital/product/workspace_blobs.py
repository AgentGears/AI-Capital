from __future__ import annotations

import os
from pathlib import Path

from ..kernel.durable_program import ProgramRepository
from ..kernel.errors import IntegrityViolation, InvalidRequest
from .workspace_types import ARTIFACT_PREFIX, WorkspaceArtifact, sha256_bytes, validate_digest


class WorkspaceBlobStore:
    """Content-addressed exact bytes for workspace snapshots and imported bundles."""

    def __init__(self, programs: ProgramRepository, root: str | Path):
        self._programs = programs
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, digest: str) -> Path:
        try:
            validate_digest(digest, field="artifact digest")
        except InvalidRequest as exc:
            raise IntegrityViolation("workspace artifact digest is invalid") from exc
        return self.root / digest[:2] / digest[2:]

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def store_file(self, content: bytes, digest: str) -> None:
        if type(content) is not bytes or sha256_bytes(content) != digest:
            raise IntegrityViolation("workspace artifact bytes do not match digest")
        path = self._path(digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            existing = path.read_bytes()
            if existing != content or sha256_bytes(existing) != digest:
                raise IntegrityViolation("content-addressed workspace artifact collision")
            return
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            with open(temporary, "xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            self._fsync_directory(path.parent)
        finally:
            if temporary.exists():
                temporary.unlink()

    def register_metadata(self, entry: WorkspaceArtifact) -> None:
        self._programs._db.execute(
            """
            INSERT INTO workspace_artifacts(artifact_digest, content_ref, byte_length)
            VALUES (?, ?, ?)
            ON CONFLICT(artifact_digest) DO NOTHING
            """,
            (entry.digest, entry.content_ref, entry.byte_length),
        )
        row = self._programs._db.execute(
            """
            SELECT content_ref, byte_length FROM workspace_artifacts
            WHERE artifact_digest = ?
            """,
            (entry.digest,),
        ).fetchone()
        if (
            row is None
            or row["content_ref"] != entry.content_ref
            or type(row["byte_length"]) is not int
            or row["byte_length"] != entry.byte_length
        ):
            raise IntegrityViolation("workspace artifact metadata collision")

    def read(self, digest: str, *, expected_length: int) -> bytes:
        row = self._programs._db.execute(
            """
            SELECT content_ref, byte_length FROM workspace_artifacts
            WHERE artifact_digest = ?
            """,
            (digest,),
        ).fetchone()
        if (
            row is None
            or row["content_ref"] != f"{ARTIFACT_PREFIX}{digest}"
            or type(row["byte_length"]) is not int
            or row["byte_length"] != expected_length
        ):
            raise IntegrityViolation("workspace artifact metadata mismatch")
        try:
            content = self._path(digest).read_bytes()
        except OSError as exc:
            raise IntegrityViolation("workspace artifact cannot be read") from exc
        if len(content) != expected_length or sha256_bytes(content) != digest:
            raise IntegrityViolation("workspace artifact content authentication failed")
        return content
