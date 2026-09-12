from __future__ import annotations

import os
import stat
from pathlib import Path

from ..kernel.errors import InvalidRequest, PersistenceConflict
from .workspace_types import (
    ARTIFACT_PREFIX,
    WorkspaceArtifact,
    canonical_artifact_path,
    sha256_bytes,
)


def _workspace_paths(root: Path) -> tuple[Path, ...]:
    if not root.exists() or not root.is_dir():
        raise InvalidRequest("workspace root does not exist or is not a directory")
    paths: list[Path] = []
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        dirs.sort()
        files.sort()
        current_path = Path(current)
        for name in dirs:
            mode = os.lstat(current_path / name).st_mode
            if stat.S_ISLNK(mode):
                raise InvalidRequest("workspace snapshots reject symbolic links")
            if not stat.S_ISDIR(mode):
                raise InvalidRequest(
                    "workspace snapshots accept directories and regular files only"
                )
        for name in files:
            candidate = current_path / name
            mode = os.lstat(candidate).st_mode
            if stat.S_ISLNK(mode):
                raise InvalidRequest("workspace snapshots reject symbolic links")
            if not stat.S_ISREG(mode):
                raise InvalidRequest("workspace snapshots accept regular files only")
            paths.append(candidate)
    paths.sort(key=lambda item: item.relative_to(root).as_posix())
    return tuple(paths)


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_mode == right.st_mode
    )


def _read_stable_regular_file(path: Path, *, relative: str) -> bytes:
    """Read one file while refusing symlink replacement and identity/content races."""
    try:
        before = os.lstat(path)
    except OSError as exc:
        raise PersistenceConflict(
            f"workspace file changed during snapshot capture: {relative}"
        ) from exc
    if stat.S_ISLNK(before.st_mode):
        raise InvalidRequest("workspace snapshots reject symbolic links")
    if not stat.S_ISREG(before.st_mode):
        raise InvalidRequest("workspace snapshots accept regular files only")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        flags |= nofollow
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        # On platforms with O_NOFOLLOW, a last-component symlink race lands here.
        raise PersistenceConflict(
            f"workspace file changed during snapshot capture: {relative}"
        ) from exc
    try:
        opened_before = os.fstat(descriptor)
        if not stat.S_ISREG(opened_before.st_mode) or not _same_identity(
            before, opened_before
        ):
            raise PersistenceConflict(
                f"workspace file changed during snapshot capture: {relative}"
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        opened_after = os.fstat(descriptor)
    except OSError as exc:
        raise PersistenceConflict(
            f"workspace file changed during snapshot capture: {relative}"
        ) from exc
    finally:
        os.close(descriptor)

    try:
        after = os.lstat(path)
    except OSError as exc:
        raise PersistenceConflict(
            f"workspace file changed during snapshot capture: {relative}"
        ) from exc
    if stat.S_ISLNK(after.st_mode) or not stat.S_ISREG(after.st_mode):
        raise PersistenceConflict(
            f"workspace file changed during snapshot capture: {relative}"
        )
    if not _same_identity(before, opened_before) or not _same_identity(
        opened_before, opened_after
    ) or not _same_identity(opened_after, after):
        raise PersistenceConflict(
            f"workspace file changed during snapshot capture: {relative}"
        )
    if (
        opened_before.st_size != opened_after.st_size
        or opened_before.st_mtime_ns != opened_after.st_mtime_ns
        or after.st_size != opened_after.st_size
        or after.st_mtime_ns != opened_after.st_mtime_ns
    ):
        raise PersistenceConflict(
            f"workspace file changed during snapshot capture: {relative}"
        )
    content = b"".join(chunks)
    if len(content) != opened_after.st_size:
        raise PersistenceConflict(
            f"workspace file changed during snapshot capture: {relative}"
        )
    return content


def capture_workspace(root: str | Path) -> tuple[tuple[WorkspaceArtifact, bytes], ...]:
    workspace = Path(root).resolve()
    paths = _workspace_paths(workspace)
    captured: list[tuple[WorkspaceArtifact, bytes]] = []
    for path in paths:
        relative = canonical_artifact_path(path.relative_to(workspace).as_posix())
        content = _read_stable_regular_file(path, relative=relative)
        digest = sha256_bytes(content)
        captured.append(
            (
                WorkspaceArtifact(
                    path=relative,
                    digest=digest,
                    byte_length=len(content),
                    content_ref=f"{ARTIFACT_PREFIX}{digest}",
                ),
                content,
            )
        )
    if tuple(path.relative_to(workspace).as_posix() for path in paths) != tuple(
        path.relative_to(workspace).as_posix()
        for path in _workspace_paths(workspace)
    ):
        raise PersistenceConflict("workspace changed during snapshot capture")
    for entry, original in captured:
        current = _read_stable_regular_file(
            workspace / Path(entry.path),
            relative=entry.path,
        )
        if current != original:
            raise PersistenceConflict(
                f"workspace file changed during snapshot capture: {entry.path}"
            )
    return tuple(captured)
