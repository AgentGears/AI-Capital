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


def capture_workspace(root: str | Path) -> tuple[tuple[WorkspaceArtifact, bytes], ...]:
    workspace = Path(root).resolve()
    paths = _workspace_paths(workspace)
    captured: list[tuple[WorkspaceArtifact, bytes]] = []
    for path in paths:
        relative = canonical_artifact_path(path.relative_to(workspace).as_posix())
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise PersistenceConflict(
                f"workspace file changed during snapshot capture: {relative}"
            ) from exc
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
        try:
            current = (workspace / Path(entry.path)).read_bytes()
        except OSError as exc:
            raise PersistenceConflict(
                f"workspace file changed during snapshot capture: {entry.path}"
            ) from exc
        if current != original:
            raise PersistenceConflict(
                f"workspace file changed during snapshot capture: {entry.path}"
            )
    return tuple(captured)
