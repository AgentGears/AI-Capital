from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shlex
import stat
import subprocess
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from ..kernel.enums import EffectClass, EffectStatus, ExecutionOutcome
from ..kernel.errors import ExecutionFailure, ExecutionTimeout, InvalidRequest
from ..kernel.models import ResolvedEffect
from ..kernel.operation_journal import ExecutionObservation
from ..kernel.serialization import canonical_json
from .workspace_types import canonical_artifact_path


_MAX_OBSERVATION_BYTES = 1024 * 1024
_COMMAND_TIMEOUT_SECONDS = 15
_HTTP_TIMEOUT_SECONDS = 15
_READ_ONLY_COMMANDS = frozenset({"pwd", "ls", "cat", "head", "tail", "wc", "stat"})


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


def _safe_existing(root: Path, target: str, *, allow_root: bool = True) -> Path:
    target = _canonical_relative(target, allow_root=allow_root)
    root = root.resolve()
    current = root
    if target == ".":
        return root
    for index, part in enumerate(PurePosixPath(target).parts):
        current = current / part
        try:
            info = os.lstat(current)
        except OSError as exc:
            raise InvalidRequest(f"capability path does not exist: {target}") from exc
        if stat.S_ISLNK(info.st_mode):
            raise InvalidRequest("capability path cannot traverse a symlink")
        if index < len(PurePosixPath(target).parts) - 1 and not stat.S_ISDIR(info.st_mode):
            raise InvalidRequest("capability path parent is not a directory")
    return current


def _safe_write_target(root: Path, target: str, *, artifact: bool = False) -> Path:
    if artifact:
        target = canonical_artifact_path(target)
    else:
        target = _canonical_relative(target, allow_root=False)
    root = root.resolve()
    candidate = root.joinpath(*PurePosixPath(target).parts)
    parent_relative = PurePosixPath(target).parent.as_posix()
    parent = root if parent_relative == "." else _safe_existing(root, parent_relative)
    if not parent.is_dir():
        raise InvalidRequest("capability target parent is not a directory")
    if candidate.exists() or candidate.is_symlink():
        info = os.lstat(candidate)
        if stat.S_ISLNK(info.st_mode):
            raise InvalidRequest("capability target cannot be a symlink")
        if not stat.S_ISREG(info.st_mode):
            raise InvalidRequest("capability target must be a regular file")
    return candidate


def _atomic_write(path: Path, content: bytes) -> None:
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    except OSError as exc:
        raise ExecutionFailure("capability write failed") from exc
    finally:
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass


def _success(output: dict[str, object]) -> ExecutionObservation:
    return ExecutionObservation(
        ExecutionOutcome.SUCCEEDED,
        EffectStatus.CONFIRMED,
        output,
    )


def _failed(output: dict[str, object], code: str) -> ExecutionObservation:
    return ExecutionObservation(
        ExecutionOutcome.FAILED,
        EffectStatus.NO_EFFECT,
        output,
        error_code=code,
    )


class ProductCapabilityExecutor:
    """Local product adapter that executes one already-authorized semantic Capability."""

    supports_idempotency = False

    def __init__(
        self,
        capability_id: str,
        *,
        workspace_root: Path,
        artifact_root: Path,
    ):
        self._capability_id = capability_id
        self._workspace_root = workspace_root.resolve()
        self._artifact_root = artifact_root.resolve()
        self._workspace_root.mkdir(parents=True, exist_ok=True)
        self._artifact_root.mkdir(parents=True, exist_ok=True)

    def execute(
        self,
        effect: ResolvedEffect,
        *,
        idempotency_key: str | None,
    ) -> ExecutionObservation:
        if idempotency_key is not None:
            raise InvalidRequest("product capability executors do not accept idempotency keys")
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
            handler = handlers[self._capability_id]
        except KeyError as exc:
            raise InvalidRequest(
                f"no product executor for Capability: {self._capability_id}"
            ) from exc
        return handler(effect)

    @staticmethod
    def _require_effect(
        effect: ResolvedEffect,
        *,
        resource_type: str,
        effect_class: EffectClass,
    ) -> None:
        if (
            effect.resource_type != resource_type
            or effect.effect_class is not effect_class
        ):
            raise InvalidRequest("authorized effect does not match product executor contract")

    def _workspace_read(self, effect: ResolvedEffect) -> ExecutionObservation:
        self._require_effect(
            effect,
            resource_type="workspace_path",
            effect_class=EffectClass.OBSERVE,
        )
        path = _safe_existing(self._workspace_root, effect.target, allow_root=False)
        try:
            info = os.lstat(path)
            if not stat.S_ISREG(info.st_mode):
                raise InvalidRequest("workspace.read target must be a regular file")
            content = path.read_bytes()
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ExecutionFailure("workspace.read requires UTF-8 text") from exc
        except OSError as exc:
            raise ExecutionFailure("workspace.read failed") from exc
        return _success(
            {
                "path": effect.target,
                "content": text,
                "byte_length": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )

    def _workspace_list(self, effect: ResolvedEffect) -> ExecutionObservation:
        self._require_effect(
            effect,
            resource_type="workspace_path",
            effect_class=EffectClass.OBSERVE,
        )
        path = _safe_existing(self._workspace_root, effect.target)
        if not path.is_dir():
            raise InvalidRequest("workspace.list target must be a directory")
        entries: list[dict[str, object]] = []
        try:
            for child in sorted(path.iterdir(), key=lambda item: item.name):
                info = os.lstat(child)
                if stat.S_ISLNK(info.st_mode):
                    kind = "symlink"
                    size = 0
                elif stat.S_ISDIR(info.st_mode):
                    kind = "directory"
                    size = 0
                elif stat.S_ISREG(info.st_mode):
                    kind = "file"
                    size = int(info.st_size)
                else:
                    kind = "special"
                    size = 0
                entries.append({"name": child.name, "kind": kind, "byte_length": size})
        except OSError as exc:
            raise ExecutionFailure("workspace.list failed") from exc
        return _success({"path": effect.target, "entries": entries})

    def _workspace_write(self, effect: ResolvedEffect) -> ExecutionObservation:
        self._require_effect(
            effect,
            resource_type="workspace_path",
            effect_class=EffectClass.MODIFY,
        )
        content = effect.parameters.get("content")
        if type(content) is not str:
            raise InvalidRequest("workspace.write content is invalid")
        path = _safe_write_target(self._workspace_root, effect.target)
        exact = content.encode("utf-8")
        _atomic_write(path, exact)
        return _success(
            {
                "path": effect.target,
                "byte_length": len(exact),
                "sha256": hashlib.sha256(exact).hexdigest(),
            }
        )

    def _command_observe(self, effect: ResolvedEffect) -> ExecutionObservation:
        self._require_effect(
            effect,
            resource_type="command",
            effect_class=EffectClass.OBSERVE,
        )
        try:
            parts = shlex.split(effect.target, posix=True)
        except ValueError as exc:
            raise InvalidRequest("command.observe command cannot be parsed") from exc
        if not parts or parts[0] not in _READ_ONLY_COMMANDS:
            raise InvalidRequest("command.observe is outside the read-only product profile")
        command = parts[0]
        operands = parts[1:]
        if command == "pwd":
            if operands:
                raise InvalidRequest("pwd does not accept arguments in the product profile")
        elif command == "ls":
            if len(operands) > 1:
                raise InvalidRequest("ls accepts at most one path in the product profile")
            if operands:
                _safe_existing(self._workspace_root, operands[0])
        else:
            if not operands:
                raise InvalidRequest(f"{command} requires a workspace path")
            for operand in operands:
                if operand.startswith("-"):
                    raise InvalidRequest("shell options are not admitted by the product profile")
                _safe_existing(self._workspace_root, operand, allow_root=False)
        try:
            completed = subprocess.run(
                parts,
                cwd=self._workspace_root,
                shell=False,
                check=False,
                capture_output=True,
                text=True,
                timeout=_COMMAND_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise ExecutionTimeout("read-only command timed out") from exc
        except OSError as exc:
            raise ExecutionFailure("read-only command could not be started") from exc
        output = {
            "command": effect.target,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
        if completed.returncode != 0:
            return _failed(output, "command_failed")
        return _success(output)

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
        response = None
        try:
            try:
                response = urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS)
            except HTTPError as exc:
                response = exc
            content = response.read(_MAX_OBSERVATION_BYTES + 1)
            if len(content) > _MAX_OBSERVATION_BYTES:
                raise ExecutionFailure("network.fetch response exceeds product observation bound")
            status = int(getattr(response, "status", response.getcode()))
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
            }
        )

    def _git_observe(self, effect: ResolvedEffect) -> ExecutionObservation:
        self._require_effect(
            effect,
            resource_type="git_repository",
            effect_class=EffectClass.OBSERVE,
        )
        repository = _safe_existing(self._workspace_root, effect.target)
        if not repository.is_dir():
            raise InvalidRequest("git.observe target must be a directory")
        operation = effect.parameters.get("operation")
        commands = {
            "status": ["git", "status", "--short", "--branch"],
            "diff": ["git", "diff", "--no-ext-diff"],
            "log": ["git", "log", "-n", "20", "--pretty=format:%H%x09%s"],
        }
        try:
            argv = commands[operation]
        except (KeyError, TypeError) as exc:
            raise InvalidRequest("git.observe operation is invalid") from exc
        environment = os.environ.copy()
        environment.update({"GIT_PAGER": "cat", "PAGER": "cat", "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"})
        try:
            completed = subprocess.run(
                argv,
                cwd=repository,
                shell=False,
                check=False,
                capture_output=True,
                text=True,
                timeout=_COMMAND_TIMEOUT_SECONDS,
                env=environment,
            )
        except subprocess.TimeoutExpired as exc:
            raise ExecutionTimeout("git observation timed out") from exc
        except OSError as exc:
            raise ExecutionFailure("git observation could not be started") from exc
        output = {
            "path": effect.target,
            "operation": operation,
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }
        if completed.returncode != 0:
            return _failed(output, "git_observation_failed")
        return _success(output)

    def _json_read(self, effect: ResolvedEffect) -> ExecutionObservation:
        self._require_effect(
            effect,
            resource_type="structured_data_path",
            effect_class=EffectClass.OBSERVE,
        )
        path = _safe_existing(self._workspace_root, effect.target, allow_root=False)
        try:
            if not stat.S_ISREG(os.lstat(path).st_mode):
                raise InvalidRequest("structured.json.read target must be a regular file")
            exact = path.read_bytes()
            value = json.loads(exact.decode("utf-8"))
            canonical = canonical_json(value)
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ExecutionFailure("structured.json.read found invalid canonical JSON data") from exc
        except OSError as exc:
            raise ExecutionFailure("structured.json.read failed") from exc
        canonical_bytes = canonical.encode("utf-8")
        return _success(
            {
                "path": effect.target,
                "canonical_json": canonical,
                "byte_length": len(canonical_bytes),
                "sha256": hashlib.sha256(canonical_bytes).hexdigest(),
            }
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
        path = _safe_write_target(self._workspace_root, effect.target)
        _atomic_write(path, exact)
        return _success(
            {
                "path": effect.target,
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
        path = _safe_write_target(self._artifact_root, effect.target, artifact=True)
        exact = content.encode("utf-8")
        _atomic_write(path, exact)
        return _success(
            {
                "path": effect.target,
                "byte_length": len(exact),
                "sha256": hashlib.sha256(exact).hexdigest(),
            }
        )
