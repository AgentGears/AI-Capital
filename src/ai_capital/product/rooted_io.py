from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import secrets
import stat
from typing import Iterable

from ..kernel.errors import ExecutionFailure, InvalidRequest


_PRIVATE_FILE_MODE = 0o600
_READ_CHUNK = 64 * 1024


def _require_platform_support() -> None:
    if os.name == "nt":
        raise ExecutionFailure(
            "race-resistant rooted file operations are unavailable on this platform"
        )
    if not getattr(os, "O_NOFOLLOW", 0) or not getattr(os, "O_DIRECTORY", 0):
        raise ExecutionFailure(
            "race-resistant rooted file operations are unavailable on this platform"
        )
    required = (os.open, os.stat, os.unlink)
    if any(function not in os.supports_dir_fd for function in required):
        raise ExecutionFailure(
            "race-resistant rooted file operations are unavailable on this platform"
        )


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_mode == right.st_mode
    )


def _open_root(
    root: Path,
    *,
    expected_identity: tuple[int, int] | None = None,
) -> int:
    _require_platform_support()
    root = Path(root)
    if not root.is_absolute():
        raise ExecutionFailure("capability root must be absolute for rooted access")
    try:
        before = os.lstat(root)
        descriptor = os.open(
            root,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
        opened = os.fstat(descriptor)
    except OSError as exc:
        raise ExecutionFailure("capability root changed during rooted access") from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISDIR(before.st_mode)
        or not stat.S_ISDIR(opened.st_mode)
        or not _same_identity(before, opened)
        or (
            expected_identity is not None
            and (int(opened.st_dev), int(opened.st_ino)) != expected_identity
        )
    ):
        os.close(descriptor)
        raise ExecutionFailure("capability root changed during rooted access")
    return descriptor


def root_identity(root: Path) -> tuple[int, int]:
    descriptor = _open_root(root)
    try:
        info = os.fstat(descriptor)
        return int(info.st_dev), int(info.st_ino)
    finally:
        os.close(descriptor)


def _parts(target: str) -> tuple[str, ...]:
    if target == ".":
        return ()
    return tuple(PurePosixPath(target).parts)


def _open_directory_from(root_fd: int, parts: Iterable[str]) -> int:
    current = os.dup(root_fd)
    try:
        for part in parts:
            next_fd = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=current,
            )
            os.close(current)
            current = next_fd
        return current
    except OSError as exc:
        os.close(current)
        raise ExecutionFailure("capability path changed during rooted access") from exc


def _open_parent(
    root: Path,
    target: str,
    *,
    expected_root_identity: tuple[int, int] | None = None,
) -> tuple[int, int, str]:
    root_fd = _open_root(root, expected_identity=expected_root_identity)
    parts = _parts(target)
    if not parts:
        os.close(root_fd)
        raise InvalidRequest("capability file target cannot be the root")
    try:
        parent_fd = _open_directory_from(root_fd, parts[:-1])
    except Exception:
        os.close(root_fd)
        raise
    return root_fd, parent_fd, parts[-1]


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short rooted write")
        view = view[written:]


def _read_bounded(descriptor: int, *, max_bytes: int) -> bytes:
    if type(max_bytes) is not int or max_bytes < 0:
        raise InvalidRequest("observation byte limit must be non-negative")
    content = bytearray()
    while True:
        remaining = max_bytes + 1 - len(content)
        if remaining <= 0:
            raise ExecutionFailure("observation exceeds product byte limit")
        chunk = os.read(descriptor, min(_READ_CHUNK, remaining))
        if not chunk:
            break
        content.extend(chunk)
        if len(content) > max_bytes:
            raise ExecutionFailure("observation exceeds product byte limit")
    return bytes(content)


def read_regular(
    root: Path,
    target: str,
    *,
    max_bytes: int,
    expected_root_identity: tuple[int, int] | None = None,
) -> bytes:
    root_fd, parent_fd, name = _open_parent(
        root, target, expected_root_identity=expected_root_identity
    )
    descriptor: int | None = None
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY
            | os.O_NOFOLLOW
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_BINARY", 0),
            dir_fd=parent_fd,
        )
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise InvalidRequest("capability observation target must be a regular file")
        if info.st_size > max_bytes:
            raise ExecutionFailure("observation exceeds product byte limit")
        return _read_bounded(descriptor, max_bytes=max_bytes)
    except FileNotFoundError as exc:
        raise InvalidRequest(f"capability path does not exist: {target}") from exc
    except OSError as exc:
        raise ExecutionFailure("capability file changed during rooted read") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_fd)
        os.close(root_fd)


def list_directory(
    root: Path,
    target: str,
    *,
    expected_root_identity: tuple[int, int] | None = None,
) -> list[dict[str, object]]:
    root_fd = _open_root(root, expected_identity=expected_root_identity)
    directory_fd: int | None = None
    try:
        directory_fd = _open_directory_from(root_fd, _parts(target))
        entries: list[dict[str, object]] = []
        for name in sorted(os.listdir(directory_fd)):
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                kind, size = "symlink", 0
            elif stat.S_ISDIR(info.st_mode):
                kind, size = "directory", 0
            elif stat.S_ISREG(info.st_mode):
                kind, size = "file", int(info.st_size)
            else:
                kind, size = "special", 0
            entries.append({"name": name, "kind": kind, "byte_length": size})
        return entries
    except FileNotFoundError as exc:
        raise InvalidRequest(f"capability path does not exist: {target}") from exc
    except OSError as exc:
        raise ExecutionFailure("capability directory changed during rooted listing") from exc
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
        os.close(root_fd)


def atomic_write(
    root: Path,
    target: str,
    content: bytes,
    *,
    expected_root_identity: tuple[int, int] | None = None,
) -> None:
    root_fd, parent_fd, name = _open_parent(
        root, target, expected_root_identity=expected_root_identity
    )
    temporary = f".{name}.{secrets.token_hex(12)}.tmp"
    descriptor: int | None = None
    created = False
    try:
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        if current is not None and not stat.S_ISREG(current.st_mode):
            raise InvalidRequest("capability target must be a regular file")
        if current is not None and stat.S_ISLNK(current.st_mode):
            raise InvalidRequest("capability target cannot be a symlink")

        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_BINARY", 0),
            _PRIVATE_FILE_MODE,
            dir_fd=parent_fd,
        )
        created = True
        _write_all(descriptor, content)
        os.fchmod(descriptor, _PRIVATE_FILE_MODE)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(
            temporary,
            name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        created = False
        os.fsync(parent_fd)
    except InvalidRequest:
        raise
    except OSError as exc:
        raise ExecutionFailure("capability write failed during rooted materialization") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if created:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except OSError:
                pass
        os.close(parent_fd)
        os.close(root_fd)


def exclusive_create(
    root: Path,
    target: str,
    content: bytes,
    *,
    expected_root_identity: tuple[int, int] | None = None,
) -> None:
    root_fd, parent_fd, name = _open_parent(
        root, target, expected_root_identity=expected_root_identity
    )
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_BINARY", 0),
            _PRIVATE_FILE_MODE,
            dir_fd=parent_fd,
        )
        created = True
        _write_all(descriptor, content)
        os.fchmod(descriptor, _PRIVATE_FILE_MODE)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.fsync(parent_fd)
    except FileExistsError as exc:
        raise InvalidRequest("capability create target already exists") from exc
    except OSError as exc:
        raise ExecutionFailure("capability create failed during rooted materialization") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if created and descriptor is not None:
            try:
                os.unlink(name, dir_fd=parent_fd)
            except OSError:
                pass
        os.close(parent_fd)
        os.close(root_fd)


def open_pinned(
    root: Path,
    target: str,
    *,
    allow_directory: bool,
    expected_root_identity: tuple[int, int] | None = None,
) -> tuple[int, int]:
    """Return (root_fd, target_fd) pinned beneath root for subprocess observation."""
    root_fd = _open_root(root, expected_identity=expected_root_identity)
    if target == ".":
        return root_fd, os.dup(root_fd)
    parts = _parts(target)
    parent_fd: int | None = None
    target_fd: int | None = None
    try:
        parent_fd = _open_directory_from(root_fd, parts[:-1])
        flags = (
            os.O_RDONLY
            | os.O_NOFOLLOW
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_BINARY", 0)
        )
        if allow_directory:
            # Do not require O_DIRECTORY: stat decides whether regular or directory is admitted.
            pass
        target_fd = os.open(parts[-1], flags, dir_fd=parent_fd)
        info = os.fstat(target_fd)
        if not stat.S_ISREG(info.st_mode) and not (
            allow_directory and stat.S_ISDIR(info.st_mode)
        ):
            raise InvalidRequest("command.observe target has unsupported file type")
        return root_fd, target_fd
    except InvalidRequest:
        if target_fd is not None:
            os.close(target_fd)
        os.close(root_fd)
        raise
    except OSError as exc:
        if target_fd is not None:
            os.close(target_fd)
        os.close(root_fd)
        raise ExecutionFailure("capability target changed during rooted observation") from exc
    finally:
        if parent_fd is not None:
            os.close(parent_fd)


def descriptor_path(descriptor: int) -> str:
    for root in (Path("/proc/self/fd"), Path("/dev/fd")):
        if root.is_dir():
            return str(root / str(descriptor))
    raise ExecutionFailure("descriptor-backed subprocess paths are unavailable")


def close_descriptors(*descriptors: int) -> None:
    seen: set[int] = set()
    for descriptor in descriptors:
        if descriptor in seen:
            continue
        seen.add(descriptor)
        try:
            os.close(descriptor)
        except OSError:
            pass
