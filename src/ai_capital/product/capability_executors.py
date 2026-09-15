from __future__ import annotations

import base64
from collections.abc import Callable
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import shutil
import stat
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ..kernel.enums import EffectClass, EffectStatus, ExecutionOutcome
from ..kernel.errors import AuthorityDenied, ExecutionFailure, ExecutionTimeout, InvalidRequest
from ..kernel.models import ResolvedEffect
from ..kernel.operation_journal import ExecutionObservation
from ..kernel.serialization import canonical_json
from .git_repository_guard import validate_git_directory_fd
from .process_observation import run_bounded_process
from .rooted_io import (
    atomic_write,
    close_descriptors,
    descriptor_path,
    exclusive_create,
    list_directory,
    open_pinned,
    read_regular,
)
from .workspace_types import canonical_artifact_path


_MAX_OBSERVATION_BYTES = 1024 * 1024
_COMMAND_TIMEOUT_SECONDS = 15
_HTTP_TIMEOUT_SECONDS = 15
_READ_ONLY_COMMANDS = frozenset({"pwd", "ls", "cat", "head", "tail", "wc", "stat"})


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _canonical_relative(value: str, *, allow_root: bool) -> str:
    if type(value) is not str or not value.strip():
        raise InvalidRequest("capability path must be non-empty")
    if "\\" in value:
        raise InvalidRequest("capability path must use POSIX separators")
    if allow_root and value == ".":
        return value
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or not candidate.parts:
        raise InvalidRequest("capability path must be relative")
    if any(part in {"", ".", ".."} for part in candidate.parts):
        raise InvalidRequest("capability path cannot contain traversal segments")
    if candidate.as_posix() != value:
        raise InvalidRequest("capability path is not canonical")
    return value


def _trusted_search_path(name: str) -> str:
    directories: list[str] = []
    if os.name == "nt":
        system_root = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR")
        if system_root:
            directories.append(str(Path(system_root) / "System32"))
        if name == "git":
            for variable in ("ProgramFiles", "ProgramFiles(x86)"):
                root = os.environ.get(variable)
                if root:
                    directories.extend(
                        (
                            str(Path(root) / "Git" / "cmd"),
                            str(Path(root) / "Git" / "bin"),
                        )
                    )
    else:
        try:
            configured = os.confstr("CS_PATH")
        except (AttributeError, OSError, ValueError):
            configured = None
        if configured:
            directories.extend(item for item in configured.split(os.pathsep) if item)
        directories.extend(("/usr/bin", "/bin", "/usr/local/bin", "/opt/homebrew/bin"))

    unique: list[str] = []
    seen: set[str] = set()
    for directory in directories:
        path = Path(directory)
        if not path.is_absolute():
            continue
        normalized = str(path.resolve(strict=False))
        if normalized in seen:
            continue
        seen.add(normalized)
        unique.append(normalized)
    return os.pathsep.join(unique)


def _inside(path: Path, root: Path) -> bool:
    root = root.resolve()
    return path == root or root in path.parents


def _success(
    output: dict[str, object],
    *,
    observational: bool = False,
) -> ExecutionObservation:
    return ExecutionObservation(
        ExecutionOutcome.SUCCEEDED,
        EffectStatus.NOT_APPLICABLE if observational else EffectStatus.CONFIRMED,
        output,
    )


def _failed(
    output: dict[str, object],
    code: str,
    *,
    observational: bool = False,
) -> ExecutionObservation:
    return ExecutionObservation(
        ExecutionOutcome.FAILED,
        EffectStatus.NOT_APPLICABLE if observational else EffectStatus.ABSENT,
        output,
        error_code=code,
    )


def _cancelled_before_dispatch(effect: ResolvedEffect) -> ExecutionObservation:
    return ExecutionObservation(
        ExecutionOutcome.CANCELLED,
        (
            EffectStatus.NOT_APPLICABLE
            if effect.effect_class is EffectClass.OBSERVE
            else EffectStatus.ABSENT
        ),
        {},
        error_code="program_not_runnable_before_dispatch",
    )


class ProductCapabilityExecutor:
    """Executes one already-authorized local product Capability."""

    supports_idempotency = False

    def __init__(
        self,
        capability_id: str,
        *,
        workspace_root: Path,
        artifact_root: Path,
        workspace_root_identity: tuple[int, int] | None = None,
        artifact_root_identity: tuple[int, int] | None = None,
        before_dispatch: Callable[[], None] | None = None,
    ):
        self._capability_id = capability_id
        self._workspace_root = Path(workspace_root)
        self._artifact_root = Path(artifact_root)
        if not self._workspace_root.is_absolute() or not self._artifact_root.is_absolute():
            raise InvalidRequest("product capability roots must be absolute")
        self._workspace_root_identity = workspace_root_identity
        self._artifact_root_identity = artifact_root_identity
        self._before_dispatch = before_dispatch

    def _trusted_executable(self, name: str) -> str:
        resolved = shutil.which(name, path=_trusted_search_path(name))
        if resolved is None:
            raise ExecutionFailure(f"trusted executable is unavailable: {name}")
        candidate = Path(resolved).resolve()
        if _inside(candidate, self._workspace_root) or _inside(candidate, self._artifact_root):
            raise ExecutionFailure("trusted executable cannot resolve inside a capability root")
        try:
            info = os.stat(candidate)
        except OSError as exc:
            raise ExecutionFailure("trusted executable cannot be inspected") from exc
        if not stat.S_ISREG(info.st_mode) or not os.access(candidate, os.X_OK):
            raise ExecutionFailure("trusted executable is not executable")
        return str(candidate)

    @staticmethod
    def _process_environment() -> dict[str, str]:
        environment = {
            key: os.environ[key]
            for key in ("SYSTEMROOT", "WINDIR", "PATHEXT")
            if key in os.environ
        }
        environment["LC_ALL"] = "C"
        return environment

    def execute(
        self,
        effect: ResolvedEffect,
        *,
        idempotency_key: str | None,
    ) -> ExecutionObservation:
        if idempotency_key is not None:
            raise InvalidRequest("product capability executors do not accept idempotency keys")
        if self._before_dispatch is not None:
            try:
                self._before_dispatch()
            except AuthorityDenied:
                return _cancelled_before_dispatch(effect)
        handlers = {
            "workspace.read": self._workspace_read,
            "workspace.list": self._workspace_list,
            "workspace.write": self._workspace_write,
            "command.observe": self._command_observe,
            "network.fetch": self._network_fetch,
            "git.observe": self._git_observe,
            "structured.json.read": self._json_read,
            "structured.json.write": self._json_write,
            "artifact.write": self._artifact_write,
        }
        try:
            return handlers[self._capability_id](effect)
        except KeyError as exc:
            raise InvalidRequest(
                f"no product executor for Capability: {self._capability_id}"
            ) from exc

    @staticmethod
    def _require_effect(
        effect: ResolvedEffect,
        *,
        resource_type: str,
        effect_class: EffectClass,
    ) -> None:
        if effect.resource_type != resource_type or effect.effect_class is not effect_class:
            raise InvalidRequest("authorized effect does not match product executor contract")

    def _workspace_read(self, effect: ResolvedEffect) -> ExecutionObservation:
        self._require_effect(
            effect,
            resource_type="workspace_path",
            effect_class=EffectClass.OBSERVE,
        )
        target = _canonical_relative(effect.target, allow_root=False)
        content = read_regular(
            self._workspace_root,
            target,
            max_bytes=_MAX_OBSERVATION_BYTES,
            expected_root_identity=self._workspace_root_identity,
        )
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ExecutionFailure("workspace.read requires UTF-8 text") from exc
        return _success(
            {
                "path": target,
                "content": text,
                "byte_length": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            },
            observational=True,
        )

    def _workspace_list(self, effect: ResolvedEffect) -> ExecutionObservation:
        self._require_effect(
            effect,
            resource_type="workspace_path",
            effect_class=EffectClass.OBSERVE,
        )
        target = _canonical_relative(effect.target, allow_root=True)
        entries = list_directory(
            self._workspace_root,
            target,
            expected_root_identity=self._workspace_root_identity,
        )
        return _success({"path": target, "entries": entries}, observational=True)

    def _workspace_write(self, effect: ResolvedEffect) -> ExecutionObservation:
        self._require_effect(
            effect,
            resource_type="workspace_path",
            effect_class=EffectClass.MODIFY,
        )
        content = effect.parameters.get("content")
        if type(content) is not str:
            raise InvalidRequest("workspace.write content is invalid")
        target = _canonical_relative(effect.target, allow_root=False)
        exact = content.encode("utf-8")
        atomic_write(
            self._workspace_root,
            target,
            exact,
            expected_root_identity=self._workspace_root_identity,
        )
        return _success(
            {
                "path": target,
                "byte_length": len(exact),
                "sha256": hashlib.sha256(exact).hexdigest(),
            }
        )

    def _command_observe(self, effect: ResolvedEffect) -> ExecutionObservation:
        self._require_effect(effect, resource_type="command", effect_class=EffectClass.OBSERVE)
        try:
            parts = shlex.split(effect.target, posix=True)
        except ValueError as exc:
            raise InvalidRequest("command.observe command cannot be parsed") from exc
        if not parts or parts[0] not in _READ_ONLY_COMMANDS:
            raise InvalidRequest("command.observe is outside the read-only product profile")
        command, operands = parts[0], parts[1:]
        if command == "pwd":
            if operands:
                raise InvalidRequest("pwd does not accept arguments in the product profile")
            operand = "."
            allow_directory = True
        elif command == "ls":
            if len(operands) > 1:
                raise InvalidRequest("ls accepts at most one path in the product profile")
            operand = "." if not operands else operands[0]
            if operand.startswith("-"):
                raise InvalidRequest("shell options are not admitted by the product profile")
            operand = _canonical_relative(operand, allow_root=True)
            allow_directory = True
        else:
            if len(operands) != 1:
                raise InvalidRequest(f"{command} accepts exactly one workspace path")
            operand = operands[0]
            if operand.startswith("-"):
                raise InvalidRequest("shell options are not admitted by the product profile")
            operand = _canonical_relative(operand, allow_root=False)
            allow_directory = command == "stat"

        root_fd, target_fd = open_pinned(
            self._workspace_root,
            operand,
            allow_directory=allow_directory,
            expected_root_identity=self._workspace_root_identity,
        )
        try:
            cwd = descriptor_path(root_fd)
            target_path = descriptor_path(target_fd)
            if command == "pwd":
                argv = ["pwd"]
            elif command == "ls" and not operands:
                argv = ["ls", target_path]
            elif command == "ls":
                argv = ["ls", target_path]
            else:
                argv = [command, target_path]
            completed = run_bounded_process(
                argv,
                cwd=cwd,
                executable=self._trusted_executable(command),
                env=self._process_environment(),
                timeout_seconds=_COMMAND_TIMEOUT_SECONDS,
                max_output_bytes=_MAX_OBSERVATION_BYTES,
                pass_fds=(root_fd, target_fd),
            )
        finally:
            close_descriptors(target_fd, root_fd)
        output = {
            "command": effect.target,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
        return (
            _success(output, observational=True)
            if completed.returncode == 0
            else _failed(output, "command_failed", observational=True)
        )

    def _network_fetch(self, effect: ResolvedEffect) -> ExecutionObservation:
        self._require_effect(
            effect,
            resource_type="network_resource",
            effect_class=EffectClass.OBSERVE,
        )
        parsed = urlsplit(effect.target)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise InvalidRequest("network.fetch requires an absolute HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None:
            raise InvalidRequest("network.fetch URL cannot contain credentials")
        request = Request(effect.target, method="GET", headers={"User-Agent": "ai-capital/0.1"})
        opener = build_opener(_NoRedirect())
        response = None
        try:
            try:
                response = opener.open(request, timeout=_HTTP_TIMEOUT_SECONDS)
            except HTTPError as exc:
                response = exc
            content = response.read(_MAX_OBSERVATION_BYTES + 1)
            if len(content) > _MAX_OBSERVATION_BYTES:
                raise ExecutionFailure("network.fetch response exceeds product observation bound")
            status = int(response.getcode())
            content_type = response.headers.get("Content-Type", "")
        except URLError as exc:
            raise ExecutionFailure("network.fetch failed") from exc
        except OSError as exc:
            raise ExecutionFailure("network.fetch failed") from exc
        finally:
            if response is not None:
                response.close()
        return _success(
            {
                "url": effect.target,
                "status": status,
                "content_type": content_type,
                "byte_length": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "content_base64": base64.b64encode(content).decode("ascii"),
            },
            observational=True,
        )

    def _git_observe(self, effect: ResolvedEffect) -> ExecutionObservation:
        self._require_effect(
            effect,
            resource_type="git_repository",
            effect_class=EffectClass.OBSERVE,
        )
        target = _canonical_relative(effect.target, allow_root=True)
        root_fd, repository_fd = open_pinned(
            self._workspace_root,
            target,
            allow_directory=True,
            expected_root_identity=self._workspace_root_identity,
        )
        git_fd: int | None = None
        try:
            if not stat.S_ISDIR(os.fstat(repository_fd).st_mode):
                raise InvalidRequest("git.observe target must be a directory")
            repository_path = descriptor_path(repository_fd)
            try:
                git_fd = os.open(
                    ".git",
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=repository_fd,
                )
            except OSError as exc:
                raise InvalidRequest("git.observe requires stable local Git metadata") from exc
            validate_git_directory_fd(git_fd)
            git_path = descriptor_path(git_fd)
            operation = effect.parameters.get("operation")
            safe_git = [
                "git",
                "--no-pager",
                f"--git-dir={git_path}",
                f"--work-tree={repository_path}",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "log.showSignature=false",
                "-c",
                "submodule.recurse=false",
            ]
            commands = {
                "status": [
                    *safe_git,
                    "status",
                    "--short",
                    "--branch",
                    "--no-ahead-behind",
                    "--ignore-submodules=all",
                ],
                "diff": [
                    *safe_git,
                    "diff-files",
                    "--raw",
                    "--no-renames",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--ignore-submodules=all",
                ],
                "log": [*safe_git, "log", "-n", "20", "--pretty=format:%H%x09%s"],
            }
            try:
                argv = commands[operation]
            except (KeyError, TypeError) as exc:
                raise InvalidRequest("git.observe operation is invalid") from exc
            executable = self._trusted_executable("git")
            with tempfile.TemporaryDirectory(prefix="ai-capital-git-home-") as isolated_home:
                environment = self._process_environment()
                environment.update(
                    {
                        "HOME": isolated_home,
                        "XDG_CONFIG_HOME": isolated_home,
                        "GIT_CONFIG_NOSYSTEM": "1",
                        "GIT_CONFIG_SYSTEM": os.devnull,
                        "GIT_CONFIG_GLOBAL": os.devnull,
                        "GIT_ATTR_NOSYSTEM": "1",
                        "GIT_PAGER": "",
                        "PAGER": "",
                        "GIT_OPTIONAL_LOCKS": "0",
                        "GIT_TERMINAL_PROMPT": "0",
                        "GIT_NO_LAZY_FETCH": "1",
                        "GIT_ALLOW_PROTOCOL": "",
                        "GIT_PROTOCOL_FROM_USER": "0",
                        "GIT_NO_REPLACE_OBJECTS": "1",
                        "GIT_COMMON_DIR": git_path,
                    }
                )
                completed = run_bounded_process(
                    argv,
                    cwd=repository_path,
                    executable=executable,
                    env=environment,
                    timeout_seconds=_COMMAND_TIMEOUT_SECONDS,
                    max_output_bytes=_MAX_OBSERVATION_BYTES,
                    pass_fds=(root_fd, repository_fd, git_fd),
                )
                validate_git_directory_fd(git_fd)
        finally:
            if git_fd is not None:
                close_descriptors(git_fd)
            close_descriptors(repository_fd, root_fd)
        output = {
            "path": target,
            "operation": operation,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
        return (
            _success(output, observational=True)
            if completed.returncode == 0
            else _failed(output, "git_observation_failed", observational=True)
        )

    def _json_read(self, effect: ResolvedEffect) -> ExecutionObservation:
        self._require_effect(
            effect,
            resource_type="structured_data_path",
            effect_class=EffectClass.OBSERVE,
        )
        target = _canonical_relative(effect.target, allow_root=False)
        exact = read_regular(
            self._workspace_root,
            target,
            max_bytes=_MAX_OBSERVATION_BYTES,
            expected_root_identity=self._workspace_root_identity,
        )
        try:
            canonical = canonical_json(json.loads(exact.decode("utf-8")))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ExecutionFailure("structured.json.read found invalid canonical JSON data") from exc
        canonical_bytes = canonical.encode("utf-8")
        return _success(
            {
                "path": target,
                "canonical_json": canonical,
                "byte_length": len(canonical_bytes),
                "sha256": hashlib.sha256(canonical_bytes).hexdigest(),
            },
            observational=True,
        )

    def _json_write(self, effect: ResolvedEffect) -> ExecutionObservation:
        self._require_effect(
            effect,
            resource_type="structured_data_path",
            effect_class=EffectClass.MODIFY,
        )
        source = effect.parameters.get("json")
        if type(source) is not str:
            raise InvalidRequest("structured.json.write json input is invalid")
        try:
            canonical = canonical_json(json.loads(source))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise InvalidRequest("structured.json.write requires valid finite JSON") from exc
        exact = canonical.encode("utf-8")
        target = _canonical_relative(effect.target, allow_root=False)
        atomic_write(
            self._workspace_root,
            target,
            exact,
            expected_root_identity=self._workspace_root_identity,
        )
        return _success(
            {
                "path": target,
                "canonical_json": canonical,
                "byte_length": len(exact),
                "sha256": hashlib.sha256(exact).hexdigest(),
            }
        )

    def _artifact_write(self, effect: ResolvedEffect) -> ExecutionObservation:
        self._require_effect(
            effect,
            resource_type="artifact_path",
            effect_class=EffectClass.CREATE,
        )
        content = effect.parameters.get("content")
        if type(content) is not str:
            raise InvalidRequest("artifact.write content is invalid")
        target = canonical_artifact_path(effect.target)
        exact = content.encode("utf-8")
        exclusive_create(
            self._artifact_root,
            target,
            exact,
            expected_root_identity=self._artifact_root_identity,
        )
        return _success(
            {
                "path": target,
                "byte_length": len(exact),
                "sha256": hashlib.sha256(exact).hexdigest(),
            }
        )
