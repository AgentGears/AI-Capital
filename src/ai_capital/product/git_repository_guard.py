from __future__ import annotations

import os
from pathlib import Path
import stat

from ..kernel.errors import InvalidRequest
from .workspace_capture import _read_stable_regular_file


_UTF8_BOM = b"\xef\xbb\xbf"


def _exists(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise InvalidRequest("Git metadata cannot be inspected") from exc
    return True


def _validate_config(content: bytes) -> None:
    if content.startswith(_UTF8_BOM):
        raise InvalidRequest("Git config cannot contain a UTF-8 BOM")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidRequest("Git config must be UTF-8") from exc
    section = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("["):
            closing = line.find("]")
            if closing < 0:
                raise InvalidRequest("Git config is malformed")
            header = line[1:closing].strip().lower()
            section = header.split(None, 1)[0].split(".", 1)[0]
            if section in {
                "alias",
                "diff",
                "filter",
                "gpg",
                "include",
                "includeif",
                "pager",
            }:
                raise InvalidRequest("Git config is outside the read-only observation profile")
            continue
        key = line.split("=", 1)[0].strip().lower()
        if section == "core" and key in {
            "alternaterefscommand",
            "fsmonitor",
            "hookspath",
            "pager",
            "worktree",
        }:
            raise InvalidRequest("Git config changes repository routing or execution")
        if section == "extensions" and key == "partialclone":
            raise InvalidRequest("Git config permits implicit object fetching")
        if section == "log" and key == "showsignature":
            raise InvalidRequest("Git config changes observation execution")
        if section == "remote" and key in {"partialclonefilter", "promisor"}:
            raise InvalidRequest("Git config permits implicit object fetching")


def _validate_metadata_tree(git_dir: Path) -> None:
    for current, dirs, files in os.walk(git_dir, topdown=True, followlinks=False):
        current_path = Path(current)
        dirs.sort()
        files.sort()
        for name in dirs:
            info = os.lstat(current_path / name)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise InvalidRequest("git.observe rejects indirect Git metadata")
        for name in files:
            info = os.lstat(current_path / name)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise InvalidRequest("git.observe rejects indirect Git metadata")


def validate_git_repository(repository: Path) -> None:
    git_dir = repository / ".git"
    try:
        info = os.lstat(git_dir)
    except OSError as exc:
        raise InvalidRequest("git.observe requires a local .git directory") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise InvalidRequest("git.observe rejects indirect Git metadata")

    for forbidden in (
        git_dir / "commondir",
        git_dir / "config.worktree",
        git_dir / "objects" / "info" / "alternates",
        git_dir / "objects" / "info" / "http-alternates",
    ):
        if _exists(forbidden):
            raise InvalidRequest("git.observe rejects routed Git metadata")

    _validate_metadata_tree(git_dir)
    config = git_dir / "config"
    try:
        info = os.lstat(config)
    except OSError as exc:
        raise InvalidRequest("Git config is missing") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise InvalidRequest("Git config must be a regular file")
    _validate_config(_read_stable_regular_file(config, relative=".git/config"))


def _read_stable_regular_at(parent_fd: int, name: str) -> bytes:
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise InvalidRequest("Git config must be a regular file")
        descriptor = os.open(
            name,
            os.O_RDONLY
            | os.O_NOFOLLOW
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_BINARY", 0),
            dir_fd=parent_fd,
        )
    except OSError as exc:
        raise InvalidRequest("Git config cannot be inspected") from exc
    try:
        opened_before = os.fstat(descriptor)
        if not stat.S_ISREG(opened_before.st_mode) or not (
            before.st_dev == opened_before.st_dev
            and before.st_ino == opened_before.st_ino
            and before.st_mode == opened_before.st_mode
        ):
            raise InvalidRequest("Git config changed during validation")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        opened_after = os.fstat(descriptor)
        after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise InvalidRequest("Git config changed during validation") from exc
    finally:
        os.close(descriptor)
    if (
        opened_before.st_dev != opened_after.st_dev
        or opened_before.st_ino != opened_after.st_ino
        or opened_before.st_mode != opened_after.st_mode
        or opened_after.st_dev != after.st_dev
        or opened_after.st_ino != after.st_ino
        or opened_after.st_mode != after.st_mode
        or opened_before.st_size != opened_after.st_size
        or opened_before.st_mtime_ns != opened_after.st_mtime_ns
        or opened_before.st_ctime_ns != opened_after.st_ctime_ns
        or opened_after.st_size != after.st_size
        or opened_after.st_mtime_ns != after.st_mtime_ns
        or opened_after.st_ctime_ns != after.st_ctime_ns
    ):
        raise InvalidRequest("Git config changed during validation")
    content = b"".join(chunks)
    if len(content) != opened_after.st_size:
        raise InvalidRequest("Git config changed during validation")
    return content


def _metadata_entry_exists(directory_fd: int, parts: tuple[str, ...]) -> bool:
    current = os.dup(directory_fd)
    try:
        for part in parts[:-1]:
            try:
                next_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=current,
                )
            except FileNotFoundError:
                return False
            except OSError as exc:
                raise InvalidRequest("Git metadata cannot be inspected") from exc
            os.close(current)
            current = next_fd
        try:
            os.stat(parts[-1], dir_fd=current, follow_symlinks=False)
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise InvalidRequest("Git metadata cannot be inspected") from exc
        return True
    finally:
        os.close(current)


def _validate_metadata_tree_fd(directory_fd: int) -> None:
    try:
        names = sorted(os.listdir(directory_fd))
    except OSError as exc:
        raise InvalidRequest("Git metadata cannot be inspected") from exc
    for name in names:
        try:
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as exc:
            raise InvalidRequest("Git metadata cannot be inspected") from exc
        if stat.S_ISLNK(info.st_mode):
            raise InvalidRequest("git.observe rejects indirect Git metadata")
        if stat.S_ISDIR(info.st_mode):
            try:
                child_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
            except OSError as exc:
                raise InvalidRequest("Git metadata changed during validation") from exc
            try:
                opened = os.fstat(child_fd)
                if (
                    info.st_dev != opened.st_dev
                    or info.st_ino != opened.st_ino
                    or info.st_mode != opened.st_mode
                ):
                    raise InvalidRequest("Git metadata changed during validation")
                _validate_metadata_tree_fd(child_fd)
            finally:
                os.close(child_fd)
            continue
        if not stat.S_ISREG(info.st_mode):
            raise InvalidRequest("git.observe rejects indirect Git metadata")


def validate_git_directory_fd(git_fd: int) -> None:
    try:
        info = os.fstat(git_fd)
    except OSError as exc:
        raise InvalidRequest("git.observe requires stable local Git metadata") from exc
    if not stat.S_ISDIR(info.st_mode):
        raise InvalidRequest("git.observe requires stable local Git metadata")
    for forbidden in (
        ("commondir",),
        ("config.worktree",),
        ("objects", "info", "alternates"),
        ("objects", "info", "http-alternates"),
    ):
        if _metadata_entry_exists(git_fd, forbidden):
            raise InvalidRequest("git.observe rejects routed Git metadata")
    _validate_metadata_tree_fd(git_fd)
    _validate_config(_read_stable_regular_at(git_fd, "config"))
