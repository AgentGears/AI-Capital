from __future__ import annotations

import os
from pathlib import Path
import stat

from ..kernel.errors import InvalidRequest
from .workspace_capture import _read_stable_regular_file


def _exists(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise InvalidRequest("Git metadata cannot be inspected") from exc
    return True


def _validate_config(content: bytes) -> None:
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
            if section in {"filter", "include", "includeif", "diff", "gpg"}:
                raise InvalidRequest("Git config is outside the read-only observation profile")
            continue
        if section == "core":
            key = line.split("=", 1)[0].strip().lower()
            if key in {"worktree", "fsmonitor", "hookspath"}:
                raise InvalidRequest("Git config changes repository routing")
        if section == "log":
            key = line.split("=", 1)[0].strip().lower()
            if key == "showsignature":
                raise InvalidRequest("Git config changes observation execution")


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
