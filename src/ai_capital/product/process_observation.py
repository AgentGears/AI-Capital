from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
from typing import Mapping, Sequence

from ..kernel.errors import ExecutionFailure, ExecutionTimeout, InvalidRequest


_READ_CHUNK = 64 * 1024
_TERMINATION_GRACE_SECONDS = 1.0
_POLL_SECONDS = 0.01


@dataclass(frozen=True, slots=True)
class BoundedProcessResult:
    returncode: int
    stdout: str
    stderr: str


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name != "nt":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
    except (OSError, ProcessLookupError):
        pass
    try:
        process.wait(timeout=_TERMINATION_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        if os.name != "nt":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except (OSError, ProcessLookupError):
        pass
    try:
        process.wait(timeout=_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise ExecutionFailure("observation subprocess could not be terminated") from exc


def run_bounded_process(
    argv: Sequence[str],
    *,
    executable: str,
    cwd: str | Path,
    env: Mapping[str, str],
    timeout_seconds: float,
    max_output_bytes: int,
    pass_fds: tuple[int, ...] = (),
) -> BoundedProcessResult:
    if not argv or any(type(item) is not str or not item for item in argv):
        raise InvalidRequest("observation subprocess argv is invalid")
    if timeout_seconds <= 0:
        raise InvalidRequest("observation timeout must be positive")
    if type(max_output_bytes) is not int or max_output_bytes < 0:
        raise InvalidRequest("observation output limit must be non-negative")
    if os.name == "nt" and pass_fds:
        raise ExecutionFailure("descriptor-backed subprocess observation is unavailable")

    kwargs: dict[str, object] = {
        "cwd": str(cwd),
        "executable": executable,
        "shell": False,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "env": dict(env),
        "start_new_session": os.name != "nt",
    }
    if os.name != "nt":
        kwargs["pass_fds"] = pass_fds
    try:
        process = subprocess.Popen(list(argv), **kwargs)  # type: ignore[arg-type]
    except OSError as exc:
        raise ExecutionFailure("observation subprocess could not be started") from exc

    assert process.stdout is not None
    assert process.stderr is not None
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    overflow = threading.Event()
    lock = threading.Lock()
    total = 0

    def read_stream(stream, sink: list[bytes]) -> None:
        nonlocal total
        try:
            while True:
                chunk = stream.read(_READ_CHUNK)
                if not chunk:
                    return
                with lock:
                    available = max_output_bytes - total
                    if available > 0:
                        sink.append(chunk[:available])
                    total += len(chunk)
                    if total > max_output_bytes:
                        overflow.set()
        except OSError:
            return

    stdout_reader = threading.Thread(
        target=read_stream,
        args=(process.stdout, stdout_chunks),
        daemon=True,
    )
    stderr_reader = threading.Thread(
        target=read_stream,
        args=(process.stderr, stderr_chunks),
        daemon=True,
    )
    stdout_reader.start()
    stderr_reader.start()

    deadline = time.monotonic() + timeout_seconds
    timed_out = False
    try:
        while process.poll() is None:
            if overflow.is_set():
                _terminate_process_group(process)
                break
            if time.monotonic() >= deadline:
                timed_out = True
                _terminate_process_group(process)
                break
            time.sleep(_POLL_SECONDS)
    finally:
        if process.poll() is None:
            _terminate_process_group(process)
        stdout_reader.join(timeout=_TERMINATION_GRACE_SECONDS)
        stderr_reader.join(timeout=_TERMINATION_GRACE_SECONDS)
        try:
            process.stdout.close()
        except OSError:
            pass
        try:
            process.stderr.close()
        except OSError:
            pass

    if stdout_reader.is_alive() or stderr_reader.is_alive():
        raise ExecutionFailure("observation subprocess output readers did not terminate")
    if timed_out:
        raise ExecutionTimeout("observation subprocess timed out")
    if overflow.is_set():
        raise ExecutionFailure("observation subprocess output exceeds product byte limit")
    if process.returncode is None:
        raise ExecutionFailure("observation subprocess has no terminal return code")

    return BoundedProcessResult(
        returncode=int(process.returncode),
        stdout=b"".join(stdout_chunks).decode("utf-8", errors="replace"),
        stderr=b"".join(stderr_chunks).decode("utf-8", errors="replace"),
    )
