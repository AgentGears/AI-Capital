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
    required = (os.open, os.stat, os.unlink, os.link)
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


def root_identity(root: Path) -> tuple[int, int] | None:
    if os.name == "nt":
        return None
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


def _descriptor_identity(descriptor: int) -> os.stat_result:
    try:
        return os.fstat(descriptor)
    except OSError as exc:
        raise ExecutionFailure("capability path changed during rooted access") from exc


def _validate_directory_binding(
    root: Path,
    parts: tuple[str, ...],
    *,
    root_fd: int,
    directory_fd: int,
    expected_root_identity: tuple[int, int] | None,
) -> None:
    current_root = _open_root(root, expected_identity=expected_root_identity)
    current_directory: int | None = None
    try:
        if not _same_identity(
            _descriptor_identity(root_fd),
            _descriptor_identity(current_root),
        ):
            raise ExecutionFailure("capability root changed during rooted access")
        current_directory = _open_directory_from(current_root, parts)
        if not _same_identity(
            _descriptor_identity(directory_fd),
            _descriptor_identity(current_directory),
        ):
            raise ExecutionFailure("capability parent changed during rooted access")
    finally:
        if current_directory is not None:
            os.close(current_directory)
        os.close(current_root)


def _validate_parent_binding(
    root: Path,
    target: str,
    *,
    root_fd: int,
    parent_fd: int,
    expected_root_identity: tuple[int, int] | None,
) -> None:
    parts = _parts(target)
    if not parts:
        raise InvalidRequest("capability file target cannot be the root")
    _validate_directory_binding(
        root,
        parts[:-1],
        root_fd=root_fd,
        directory_fd=parent_fd,
        expected_root_identity=expected_root_identity,
    )


def validate_pinned(
    root: Path,
    target: str,
    *,
    root_fd: int,
    target_fd: int,
    allow_directory: bool,
    expected_root_identity: tuple[int, int] | None = None,
    expected_parent_fd: int | None = None,
) -> None:
    """Require the current rooted name to still identify the pinned target."""
    current_root = _open_root(root, expected_identity=expected_root_identity)
    current_parent_fd: int | None = None
    current_target: int | None = None
    try:
        if not _same_identity(
            _descriptor_identity(root_fd),
            _descriptor_identity(current_root),
        ):
            raise ExecutionFailure("capability root changed during rooted access")
        if target == ".":
            current_target = os.dup(current_root)
        else:
            parts = _parts(target)
            if not parts:
                raise InvalidRequest("capability target is invalid")
            current_parent_fd = _open_directory_from(current_root, parts[:-1])
            if expected_parent_fd is not None and not _same_identity(
                _descriptor_identity(expected_parent_fd),
                _descriptor_identity(current_parent_fd),
            ):
                raise ExecutionFailure("capability parent changed during rooted access")
            flags = (
                os.O_RDONLY
                | os.O_NOFOLLOW
                | getattr(os, "O_NONBLOCK", 0)
                | getattr(os, "O_BINARY", 0)
            )
            current_target = os.open(parts[-1], flags, dir_fd=current_parent_fd)
        pinned = _descriptor_identity(target_fd)
        current = _descriptor_identity(current_target)
        admitted_type = stat.S_ISREG(current.st_mode) or (
            allow_directory and stat.S_ISDIR(current.st_mode)
        )
        if not admitted_type or not _same_identity(pinned, current):
            raise ExecutionFailure("capability target changed during rooted access")
    except FileNotFoundError as exc:
        raise ExecutionFailure("capability target changed during rooted access") from exc
    except OSError as exc:
        raise ExecutionFailure("capability target changed during rooted access") from exc
    finally:
        if current_target is not None:
            os.close(current_target)
        if current_parent_fd is not None:
            os.close(current_parent_fd)
        os.close(current_root)

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
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise InvalidRequest("capability observation target must be a regular file")
        descriptor = os.open(
            name,
            os.O_RDONLY
            | os.O_NOFOLLOW
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_BINARY", 0),
            dir_fd=parent_fd,
        )
        opened_before = os.fstat(descriptor)
        if not stat.S_ISREG(opened_before.st_mode) or not _same_identity(
            before, opened_before
        ):
            raise ExecutionFailure("capability file changed during rooted read")
        if opened_before.st_size > max_bytes:
            raise ExecutionFailure("observation exceeds product byte limit")
        content = _read_bounded(descriptor, max_bytes=max_bytes)
        opened_after = os.fstat(descriptor)
        after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            stat.S_ISLNK(after.st_mode)
            or not stat.S_ISREG(after.st_mode)
            or not _same_identity(opened_before, opened_after)
            or not _same_identity(opened_after, after)
            or opened_before.st_size != opened_after.st_size
            or opened_before.st_mtime_ns != opened_after.st_mtime_ns
            or opened_before.st_ctime_ns != opened_after.st_ctime_ns
            or after.st_size != opened_after.st_size
            or after.st_mtime_ns != opened_after.st_mtime_ns
            or after.st_ctime_ns != opened_after.st_ctime_ns
            or len(content) != opened_after.st_size
        ):
            raise ExecutionFailure("capability file changed during rooted read")
        validate_pinned(
            root,
            target,
            root_fd=root_fd,
            target_fd=descriptor,
            allow_directory=False,
            expected_root_identity=expected_root_identity,
            expected_parent_fd=parent_fd,
        )
        return content
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
        _validate_directory_binding(
            root,
            _parts(target),
            root_fd=root_fd,
            directory_fd=directory_fd,
            expected_root_identity=expected_root_identity,
        )
        return entries
    except FileNotFoundError as exc:
        raise InvalidRequest(f"capability path does not exist: {target}") from exc
    except OSError as exc:
        raise ExecutionFailure("capability directory changed during rooted listing") from exc
    finally:
        if directory_fd is not None:
            os.close(directory_fd)
        os.close(root_fd)

def _stat_identity(info: os.stat_result) -> tuple[int, int, int]:
    return int(info.st_dev), int(info.st_ino), int(info.st_mode)



def _fresh_side_name(parent_fd: int, name: str, suffix: str) -> str:
    for _ in range(16):
        candidate = f".{name}.{secrets.token_hex(12)}.{suffix}"
        try:
            os.stat(candidate, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return candidate
        except OSError as exc:
            raise ExecutionFailure("capability write sidecar cannot be inspected") from exc
    raise ExecutionFailure("capability write sidecar identity could not be allocated")


def _unlink_if_identity(
    parent_fd: int,
    name: str,
    expected_identity: tuple[int, int, int],
) -> bool:
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return False
    if _stat_identity(current) != expected_identity:
        return False
    try:
        os.unlink(name, dir_fd=parent_fd)
    except OSError:
        return False
    return True


def _claim_existing_target(parent_fd: int, name: str, displaced: str) -> None:
    try:
        os.rename(
            name,
            displaced,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
    except TypeError as exc:
        raise ExecutionFailure(
            "race-resistant existing-target materialization is unavailable"
        ) from exc


def _restore_displaced_regular(
    parent_fd: int,
    displaced: str,
    name: str,
    expected_identity: tuple[int, int, int],
) -> bool:
    try:
        current = os.stat(displaced, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return False
    if (
        not stat.S_ISREG(current.st_mode)
        or _stat_identity(current) != expected_identity
    ):
        return False
    try:
        os.link(
            displaced,
            name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except (FileExistsError, OSError):
        return False
    if not _unlink_if_identity(parent_fd, displaced, expected_identity):
        return False
    try:
        os.fsync(parent_fd)
    except OSError:
        return False
    return True


def atomic_write(
    root: Path,
    target: str,
    content: bytes,
    *,
    expected_root_identity: tuple[int, int] | None = None,
) -> None:
    """Materialize a rooted write without mutating a pre-existing target inode."""
    root_fd, parent_fd, name = _open_parent(
        root, target, expected_root_identity=expected_root_identity
    )
    descriptor: int | None = None
    original_descriptor: int | None = None
    temporary: str | None = None
    temporary_owned = False
    temporary_identity: tuple[int, int, int] | None = None
    cleanup_created_target = False
    created_target_identity: tuple[int, int, int] | None = None
    displaced: str | None = None
    displaced_identity: tuple[int, int, int] | None = None
    preserve_displaced = False
    try:
        _validate_parent_binding(
            root,
            target,
            root_fd=root_fd,
            parent_fd=parent_fd,
            expected_root_identity=expected_root_identity,
        )
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        if current is not None and stat.S_ISLNK(current.st_mode):
            raise InvalidRequest("capability target cannot be a symlink")
        if current is not None and not stat.S_ISREG(current.st_mode):
            raise InvalidRequest("capability target must be a regular file")
        if current is not None and current.st_nlink != 1:
            raise InvalidRequest(
                "capability target with multiple hard links is not writable"
            )

        if current is None:
            temporary = f".{name}.{secrets.token_hex(12)}.tmp"
            descriptor = os.open(
                temporary,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | os.O_NOFOLLOW
                | getattr(os, "O_BINARY", 0),
                _PRIVATE_FILE_MODE,
                dir_fd=parent_fd,
            )
            temporary_owned = True
            temporary_identity = _stat_identity(os.fstat(descriptor))
            _write_all(descriptor, content)
            os.fchmod(descriptor, _PRIVATE_FILE_MODE)
            os.fsync(descriptor)
            _validate_parent_binding(
                root,
                target,
                root_fd=root_fd,
                parent_fd=parent_fd,
                expected_root_identity=expected_root_identity,
            )
            created_target_identity = _stat_identity(os.fstat(descriptor))
            try:
                os.link(
                    temporary,
                    name,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileExistsError as exc:
                raise InvalidRequest(
                    "capability target changed before rooted materialization commit"
                ) from exc
            cleanup_created_target = True
            os.unlink(temporary, dir_fd=parent_fd)
            temporary_owned = False
            os.fsync(parent_fd)
            validate_pinned(
                root,
                target,
                root_fd=root_fd,
                target_fd=descriptor,
                allow_directory=False,
                expected_root_identity=expected_root_identity,
                expected_parent_fd=parent_fd,
            )
            cleanup_created_target = False
            return

        expected_identity = _stat_identity(current)
        original_descriptor = os.open(
            name,
            os.O_RDONLY
            | os.O_NOFOLLOW
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_BINARY", 0),
            dir_fd=parent_fd,
        )
        original_opened = os.fstat(original_descriptor)
        if (
            not stat.S_ISREG(original_opened.st_mode)
            or _stat_identity(original_opened) != expected_identity
            or original_opened.st_nlink != 1
        ):
            raise ExecutionFailure(
                "capability target changed before rooted materialization"
            )
        temporary = f".{name}.{secrets.token_hex(12)}.tmp"
        descriptor = os.open(
            temporary,
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | os.O_NOFOLLOW
            | getattr(os, "O_BINARY", 0),
            _PRIVATE_FILE_MODE,
            dir_fd=parent_fd,
        )
        temporary_owned = True
        temporary_identity = _stat_identity(os.fstat(descriptor))
        _write_all(descriptor, content)
        os.fchmod(descriptor, _PRIVATE_FILE_MODE)
        os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        materialized = _read_bounded(descriptor, max_bytes=len(content))
        staged = os.fstat(descriptor)
        if (
            not stat.S_ISREG(staged.st_mode)
            or _stat_identity(staged) != temporary_identity
            or materialized != content
            or staged.st_size != len(content)
            or stat.S_IMODE(staged.st_mode) != _PRIVATE_FILE_MODE
        ):
            raise ExecutionFailure("capability staged materialization is unstable")

        # The original identity is captured before staging. Any replacement during
        # staging is therefore rejected before namespace mutation.
        _validate_parent_binding(
            root,
            target,
            root_fd=root_fd,
            parent_fd=parent_fd,
            expected_root_identity=expected_root_identity,
        )
        before_claim = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            stat.S_ISLNK(before_claim.st_mode)
            or not stat.S_ISREG(before_claim.st_mode)
            or _stat_identity(before_claim) != expected_identity
        ):
            raise ExecutionFailure("capability target changed before rooted materialization")

        # Never mutate the existing inode. Move the final directory entry aside, then
        # install the fully materialized private inode with an atomic no-overwrite link.
        # A hard-link alias created immediately before the claim retains the old bytes.
        displaced = _fresh_side_name(parent_fd, name, "previous")
        try:
            _claim_existing_target(parent_fd, name, displaced)
        except FileNotFoundError as exc:
            raise ExecutionFailure(
                "capability target changed before rooted materialization commit"
            ) from exc
        except OSError as exc:
            raise ExecutionFailure(
                "capability target could not be claimed for rooted materialization"
            ) from exc
        displaced_info = os.stat(displaced, dir_fd=parent_fd, follow_symlinks=False)
        displaced_identity = _stat_identity(displaced_info)
        if displaced_identity != expected_identity:
            if _restore_displaced_regular(
                parent_fd,
                displaced,
                name,
                displaced_identity,
            ):
                displaced = None
                displaced_identity = None
            else:
                # Preserve a raced object rather than overwrite/delete it when the
                # rooted name has already been occupied by a newer writer.
                preserve_displaced = True
            raise ExecutionFailure(
                "capability target changed at rooted materialization commit"
            )

        try:
            os.link(
                temporary,
                name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError as exc:
            # A concurrent writer won after the original entry was claimed. Leave that
            # newer rooted entry untouched and retire only the old authorized entry.
            if displaced_identity is not None:
                _unlink_if_identity(parent_fd, displaced, displaced_identity)
            displaced = None
            displaced_identity = None
            try:
                os.fsync(parent_fd)
            except OSError:
                pass
            raise ExecutionFailure(
                "capability target changed during rooted materialization commit"
            ) from exc

        created_target_identity = temporary_identity
        # Once the new inode is installed there is no namespace rollback: ambiguity
        # becomes indeterminate Operation truth instead of risking a concurrent writer.
        cleanup_created_target = False
        os.unlink(temporary, dir_fd=parent_fd)
        temporary_owned = False
        if displaced_identity is None or not _unlink_if_identity(
            parent_fd,
            displaced,
            displaced_identity,
        ):
            preserve_displaced = True
            raise ExecutionFailure(
                "capability previous target could not be retired after commit"
            )
        displaced = None
        displaced_identity = None
        os.fsync(parent_fd)

        validate_pinned(
            root,
            target,
            root_fd=root_fd,
            target_fd=descriptor,
            allow_directory=False,
            expected_root_identity=expected_root_identity,
            expected_parent_fd=parent_fd,
        )
        committed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        verified = os.fstat(descriptor)
        if (
            _stat_identity(committed) != created_target_identity
            or _stat_identity(verified) != created_target_identity
            or not stat.S_ISREG(committed.st_mode)
            or materialized != content
            or committed.st_size != len(content)
            or verified.st_size != len(content)
            or stat.S_IMODE(committed.st_mode) != _PRIVATE_FILE_MODE
            or stat.S_IMODE(verified.st_mode) != _PRIVATE_FILE_MODE
        ):
            raise ExecutionFailure(
                "capability target changed during rooted materialization commit"
            )
    except InvalidRequest:
        raise
    except ExecutionFailure:
        raise
    except OSError as exc:
        raise ExecutionFailure(
            "capability write failed during rooted materialization"
        ) from exc
    finally:
        if cleanup_created_target and created_target_identity is not None:
            if _unlink_if_identity(parent_fd, name, created_target_identity):
                try:
                    os.fsync(parent_fd)
                except OSError:
                    pass
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if original_descriptor is not None:
            try:
                os.close(original_descriptor)
            except OSError:
                pass
        if temporary_owned and temporary is not None and temporary_identity is not None:
            _unlink_if_identity(parent_fd, temporary, temporary_identity)
        if (
            displaced is not None
            and displaced_identity is not None
            and not preserve_displaced
        ):
            _unlink_if_identity(parent_fd, displaced, displaced_identity)
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
    committed = False
    created_identity: tuple[int, int, int] | None = None
    try:
        _validate_parent_binding(
            root,
            target,
            root_fd=root_fd,
            parent_fd=parent_fd,
            expected_root_identity=expected_root_identity,
        )
        descriptor = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_BINARY", 0),
            _PRIVATE_FILE_MODE,
            dir_fd=parent_fd,
        )
        created = True
        created_identity = _stat_identity(os.fstat(descriptor))
        _write_all(descriptor, content)
        os.fchmod(descriptor, _PRIVATE_FILE_MODE)
        os.fsync(descriptor)
        os.fsync(parent_fd)
        validate_pinned(
            root,
            target,
            root_fd=root_fd,
            target_fd=descriptor,
            allow_directory=False,
            expected_root_identity=expected_root_identity,
            expected_parent_fd=parent_fd,
        )
        committed = True
    except FileExistsError as exc:
        raise InvalidRequest("capability create target already exists") from exc
    except InvalidRequest:
        raise
    except ExecutionFailure:
        raise
    except OSError as exc:
        raise ExecutionFailure("capability create failed during rooted materialization") from exc
    finally:
        if created and not committed and created_identity is not None:
            try:
                current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if _stat_identity(current) == created_identity:
                    os.unlink(name, dir_fd=parent_fd)
                    try:
                        os.fsync(parent_fd)
                    except OSError:
                        pass
            except OSError:
                pass
        if descriptor is not None:
            try:
                os.close(descriptor)
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
        target_fd = os.dup(root_fd)
        try:
            validate_pinned(
                root,
                target,
                root_fd=root_fd,
                target_fd=target_fd,
                allow_directory=True,
                expected_root_identity=expected_root_identity,
            )
        except Exception:
            os.close(target_fd)
            os.close(root_fd)
            raise
        return root_fd, target_fd
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
        target_fd = os.open(parts[-1], flags, dir_fd=parent_fd)
        info = os.fstat(target_fd)
        if not stat.S_ISREG(info.st_mode) and not (
            allow_directory and stat.S_ISDIR(info.st_mode)
        ):
            raise InvalidRequest("command.observe target has unsupported file type")
        validate_pinned(
            root,
            target,
            root_fd=root_fd,
            target_fd=target_fd,
            allow_directory=allow_directory,
            expected_root_identity=expected_root_identity,
            expected_parent_fd=parent_fd,
        )
        return root_fd, target_fd
    except InvalidRequest:
        if target_fd is not None:
            os.close(target_fd)
        os.close(root_fd)
        raise
    except ExecutionFailure:
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
