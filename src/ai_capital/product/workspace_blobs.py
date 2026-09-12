from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

from ..kernel.durable_program import ProgramRepository
from ..kernel.errors import IntegrityViolation, InvalidRequest, PersistenceConflict
from .workspace_types import ARTIFACT_PREFIX, WorkspaceArtifact, sha256_bytes, validate_digest


class WorkspaceBlobStore:
    """Content-addressed exact bytes for workspace snapshots and imported bundles."""

    def __init__(self, programs: ProgramRepository, root: str | Path):
        self._programs = programs
        self.root = Path(root).resolve()
        self._mkdir_durable(self.root)

    def _path(self, digest: str) -> Path:
        try:
            validate_digest(digest, field="artifact digest")
        except InvalidRequest as exc:
            raise IntegrityViolation("workspace artifact digest is invalid") from exc
        return self.root / digest[:2] / digest[2:]

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
                f"cannot open workspace artifact directory for durable flush: {path}"
            ) from ctypes.WinError(error)
        try:
            if not flush_file_buffers(handle):
                error = ctypes.get_last_error()
                raise PersistenceConflict(
                    f"cannot durably flush workspace artifact directory: {path}"
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
                f"cannot durably replace workspace artifact: {destination}"
            ) from ctypes.WinError(error)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":
            WorkspaceBlobStore._windows_fsync_directory(path)
            return
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise PersistenceConflict(
                f"cannot open workspace artifact directory for durable flush: {path}"
            ) from exc
        try:
            os.fsync(descriptor)
        except OSError as exc:
            raise PersistenceConflict(
                f"cannot durably flush workspace artifact directory: {path}"
            ) from exc
        finally:
            os.close(descriptor)

    def _mkdir_durable(self, path: Path) -> None:
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

    def _replace_durable(self, source: Path, destination: Path) -> None:
        if os.name == "nt":
            self._windows_replace_durable(source, destination)
        else:
            os.replace(source, destination)
        self._fsync_directory(destination.parent)

    def _read_exact_path(
        self,
        path: Path,
        *,
        digest: str,
        expected_length: int,
    ) -> bytes:
        if type(expected_length) is not int or expected_length < 0:
            raise IntegrityViolation("workspace artifact byte length is invalid")
        try:
            stored_length = path.stat().st_size
        except OSError as exc:
            raise IntegrityViolation("workspace artifact metadata cannot be read") from exc
        if stored_length != expected_length:
            raise IntegrityViolation("workspace artifact byte length mismatch")
        try:
            with path.open("rb") as handle:
                content = handle.read(expected_length + 1)
        except OSError as exc:
            raise IntegrityViolation("workspace artifact cannot be read") from exc
        if len(content) != expected_length or sha256_bytes(content) != digest:
            raise IntegrityViolation("workspace artifact content authentication failed")
        return content

    def store_file(self, content: bytes, digest: str) -> None:
        if type(content) is not bytes or sha256_bytes(content) != digest:
            raise IntegrityViolation("workspace artifact bytes do not match digest")
        path = self._path(digest)
        self._mkdir_durable(path.parent)
        if path.exists():
            existing = self._read_exact_path(
                path,
                digest=digest,
                expected_length=len(content),
            )
            if existing != content:
                raise IntegrityViolation("content-addressed workspace artifact collision")
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
        return self._read_exact_path(
            self._path(digest),
            digest=digest,
            expected_length=expected_length,
        )
