from pathlib import Path


def once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected one match, found {count}")
    return text.replace(old, new, 1)


def section(text: str, start: str, end: str, replacement: str, label: str) -> str:
    left = text.find(start)
    if left < 0:
        raise SystemExit(f"{label}: start marker missing")
    right = text.find(end, left)
    if right < 0:
        raise SystemExit(f"{label}: end marker missing")
    return text[:left] + replacement + text[right:]


# 1. Bounded process termination owns the whole process group until readers drain.
path = Path("src/ai_capital/product/process_observation.py")
text = path.read_text()
old = '''def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
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
'''
new = '''def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if os.name == "nt":
        if process.poll() is not None:
            return
        try:
            process.terminate()
        except (OSError, ProcessLookupError):
            pass
        try:
            process.wait(timeout=_TERMINATION_GRACE_SECONDS)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            process.kill()
        except (OSError, ProcessLookupError):
            pass
        try:
            process.wait(timeout=_TERMINATION_GRACE_SECONDS)
        except subprocess.TimeoutExpired as exc:
            raise ExecutionFailure("observation subprocess could not be terminated") from exc
        return

    process_group_id = process.pid
    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass
    deadline = time.monotonic() + _TERMINATION_GRACE_SECONDS
    while _process_group_exists(process_group_id) and time.monotonic() < deadline:
        time.sleep(_POLL_SECONDS)
    if _process_group_exists(process_group_id):
        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    if process.poll() is None:
        try:
            process.wait(timeout=_TERMINATION_GRACE_SECONDS)
        except subprocess.TimeoutExpired as exc:
            raise ExecutionFailure("observation subprocess could not be terminated") from exc
'''
text = once(text, old, new, "process-group termination")
old = '''    deadline = time.monotonic() + timeout_seconds
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
'''
new = '''    deadline = time.monotonic() + timeout_seconds
    timed_out = False
    try:
        while True:
            leader_done = process.poll() is not None
            readers_done = not stdout_reader.is_alive() and not stderr_reader.is_alive()
            if leader_done and readers_done:
                break
            if overflow.is_set():
                _terminate_process_group(process)
                break
            if time.monotonic() >= deadline:
                timed_out = True
                _terminate_process_group(process)
                break
            time.sleep(_POLL_SECONDS)
    finally:
        if process.poll() is None or stdout_reader.is_alive() or stderr_reader.is_alive():
            _terminate_process_group(process)
        stdout_reader.join(timeout=_TERMINATION_GRACE_SECONDS)
        stderr_reader.join(timeout=_TERMINATION_GRACE_SECONDS)
'''
text = once(text, old, new, "process-group reader ownership")
path.write_text(text)


# 2. Rooted I/O: stable reads, deferred unsupported-platform rejection, and conflict-preserving atomic commit.
path = Path("src/ai_capital/product/rooted_io.py")
text = path.read_text()
text = once(text, "import os\n", "import ctypes\nimport os\n", "ctypes import")
text = once(
    text,
    "    required = (os.open, os.stat, os.unlink)\n",
    "    required = (os.open, os.stat, os.unlink, os.link)\n",
    "rooted dir-fd requirements",
)
text = once(
    text,
    '''def root_identity(root: Path) -> tuple[int, int]:
    descriptor = _open_root(root)
''',
    '''def root_identity(root: Path) -> tuple[int, int] | None:
    if os.name == "nt":
        return None
    descriptor = _open_root(root)
''',
    "deferred root identity",
)
old = '''    descriptor: int | None = None
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
'''
new = '''    descriptor: int | None = None
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
        return content
'''
text = once(text, old, new, "stable rooted read")
start = "def atomic_write(\n"
end = "def exclusive_create(\n"
replacement = '''def _stat_identity(info: os.stat_result) -> tuple[int, int, int]:
    return int(info.st_dev), int(info.st_ino), int(info.st_mode)


def _rename_exchange(parent_fd: int, left: str, right: str) -> None:
    if os.name == "nt":
        raise ExecutionFailure(
            "atomic existing-target materialization is unavailable on this platform"
        )
    library = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(library, "renameat2", None)
    if renameat2 is None:
        raise ExecutionFailure(
            "atomic existing-target materialization is unavailable on this platform"
        )
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    if renameat2(
        parent_fd,
        os.fsencode(left),
        parent_fd,
        os.fsencode(right),
        2,
    ) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


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
    temporary_owned = False
    try:
        try:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        if current is not None and stat.S_ISLNK(current.st_mode):
            raise InvalidRequest("capability target cannot be a symlink")
        if current is not None and not stat.S_ISREG(current.st_mode):
            raise InvalidRequest("capability target must be a regular file")
        expected_target_identity = (
            None if current is None else _stat_identity(current)
        )

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
        _write_all(descriptor, content)
        os.fchmod(descriptor, _PRIVATE_FILE_MODE)
        os.fsync(descriptor)
        temporary_identity = _stat_identity(os.fstat(descriptor))
        os.close(descriptor)
        descriptor = None

        if expected_target_identity is None:
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
            os.unlink(temporary, dir_fd=parent_fd)
            temporary_owned = False
            os.fsync(parent_fd)
            return

        _rename_exchange(parent_fd, temporary, name)
        temporary_owned = False
        displaced = os.stat(temporary, dir_fd=parent_fd, follow_symlinks=False)
        committed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        displaced_identity = _stat_identity(displaced)
        committed_identity = _stat_identity(committed)
        if (
            displaced_identity == expected_target_identity
            and committed_identity == temporary_identity
        ):
            os.unlink(temporary, dir_fd=parent_fd)
            os.fsync(parent_fd)
            return

        # The atomic exchange preserves the displaced concurrent target at the
        # temporary name. Roll back only while both exchange participants still
        # have the identities captured immediately after the commit point.
        rollback_target = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        rollback_displaced = os.stat(
            temporary, dir_fd=parent_fd, follow_symlinks=False
        )
        if (
            _stat_identity(rollback_target) != committed_identity
            or _stat_identity(rollback_displaced) != displaced_identity
        ):
            raise ExecutionFailure(
                "capability target changed during atomic materialization rollback"
            )
        _rename_exchange(parent_fd, temporary, name)
        temporary_owned = True
        restored = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        restored_temporary = os.stat(
            temporary, dir_fd=parent_fd, follow_symlinks=False
        )
        if (
            _stat_identity(restored) != displaced_identity
            or _stat_identity(restored_temporary) != committed_identity
        ):
            temporary_owned = False
            raise ExecutionFailure(
                "capability target changed during atomic materialization rollback"
            )
        os.unlink(temporary, dir_fd=parent_fd)
        temporary_owned = False
        os.fsync(parent_fd)
        raise InvalidRequest(
            "capability target changed at rooted materialization commit"
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
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary_owned:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except OSError:
                pass
        os.close(parent_fd)
        os.close(root_fd)


'''
text = section(text, start, end, replacement, "atomic rooted write")
path.write_text(text)


# 3. Recover interrupted Operations when an exclusive product writer is reopened.
path = Path("src/ai_capital/product/capability_operator.py")
text = path.read_text()
text = once(
    text,
    '''        self._controls = ProgramControlRepository(programs)
        self._journal = OperationJournal(programs)
        self._host = OperationHost(self._journal, self._authority)
''',
    '''        self._controls = ProgramControlRepository(programs)
        self._journal = OperationJournal(programs)
        if owns_repository:
            self._journal.recover_interrupted()
        self._host = OperationHost(self._journal, self._authority)
''',
    "capability startup recovery",
)
path.write_text(text)

path = Path("src/ai_capital/product/program_operator.py")
text = path.read_text()
text = once(
    text,
    '''        self._controls = ProgramControlRepository(programs)
        self._operations = OperationJournal(programs)
        self._audit: LocalAuditOperator | None = None
''',
    '''        self._controls = ProgramControlRepository(programs)
        self._operations = OperationJournal(programs)
        if owns_repository:
            self._operations.recover_interrupted()
        self._audit: LocalAuditOperator | None = None
''',
    "program startup recovery",
)
path.write_text(text)


# 4. Validate the already-pinned Git metadata directory by descriptor.
path = Path("src/ai_capital/product/git_repository_guard.py")
text = path.read_text().rstrip()
addition = '''


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
'''
if "def validate_git_directory_fd(" in text:
    raise SystemExit("descriptor Git validator already exists")
path.write_text(text + addition + "\n")

path = Path("src/ai_capital/product/capability_executors.py")
text = path.read_text()
text = once(
    text,
    "from .git_repository_guard import validate_git_repository\n",
    "from .git_repository_guard import validate_git_directory_fd\n",
    "descriptor Git validator import",
)
old = '''            repository_path = descriptor_path(repository_fd)
            validate_git_repository(Path(repository_path))
            try:
                git_fd = os.open(
                    ".git",
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=repository_fd,
                )
            except OSError as exc:
                raise InvalidRequest("git.observe requires stable local Git metadata") from exc
            git_path = descriptor_path(git_fd)
'''
new = '''            repository_path = descriptor_path(repository_fd)
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
'''
text = once(text, old, new, "pin Git metadata before validation")
path.write_text(text)


# 5. Regression coverage for each review finding.
path = Path("tests/test_h2_reliability_restart_matrix.py")
text = path.read_text()
old = '''            with ProgramRepository(database) as programs:
                journal = OperationJournal(programs)
                journal.recover_interrupted()
                recovered = journal.get(operation.operation_id)
            self.assertIs(recovered.execution_outcome, ExecutionOutcome.FAILED)
            self.assertIs(recovered.effect_status, EffectStatus.INDETERMINATE)
            self.assertIs(
                recovered.reconciliation_status,
                ReconciliationStatus.PENDING,
            )

            with LocalProgramOperator.open(database) as operator:
                view = operator.show("p-1")
'''
new = '''            with LocalProgramOperator.open(database) as operator:
                view = operator.show("p-1")
                recovered = operator._operations.get(operation.operation_id)
            self.assertIs(recovered.execution_outcome, ExecutionOutcome.FAILED)
            self.assertIs(recovered.effect_status, EffectStatus.INDETERMINATE)
            self.assertIs(
                recovered.reconciliation_status,
                ReconciliationStatus.PENDING,
            )

'''
text = once(text, old, new, "automatic startup recovery regression")
path.write_text(text)

path = Path("tests/test_h2_reliability_review.py")
text = path.read_text()
text = once(text, "import os\n", "import os\nimport sys\n", "review sys import")
text = once(
    text,
    "from ai_capital.kernel.enums import ProgramStatus\n",
    "from ai_capital.kernel.enums import ProgramStatus\nfrom ai_capital.kernel.errors import ExecutionTimeout\n",
    "review timeout import",
)
text = once(
    text,
    "from ai_capital.product import rooted_io\n",
    "from ai_capital.product import rooted_io\nfrom ai_capital.product.process_observation import run_bounded_process\n",
    "review bounded process import",
)
marker = "    def test_workspace_root_swap_to_symlink_fails_closed(self):\n"
addition = '''    def test_workspace_read_rejects_in_place_mutation_during_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace, artifacts = self._fixture(root)
            target = workspace / "changing.txt"
            target.write_text("before\\n")
            real_read = rooted_io._read_bounded

            def read_then_mutate(descriptor: int, *, max_bytes: int) -> bytes:
                content = real_read(descriptor, max_bytes=max_bytes)
                target.write_text("after!\\n")
                return content

            with self._open(database, workspace, artifacts) as operator:
                operator.grant(
                    actor_id="a-1",
                    capability_id="workspace.read",
                    resource_scope=("changing.txt",),
                )
                with patch(
                    "ai_capital.product.rooted_io._read_bounded",
                    side_effect=read_then_mutate,
                ):
                    result = operator.invoke(
                        program_id="p-1",
                        actor_id="a-1",
                        capability_id="workspace.read",
                        arguments={"path": "changing.txt"},
                    )
            self.assertEqual(result["operation"]["execution_outcome"], "failed")
            self.assertEqual(result["operation"]["effect_status"], "not_applicable")

    def test_timeout_kills_descendant_after_leader_exits(self):
        child = (
            "import signal,time;"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
            "time.sleep(10)"
        )
        leader = (
            "import subprocess,sys,time;"
            "subprocess.Popen([sys.executable,'-c',sys.argv[1]]);"
            "time.sleep(10)"
        )
        started = time.monotonic()
        with self.assertRaises(ExecutionTimeout):
            run_bounded_process(
                [sys.executable, "-c", leader, child],
                executable=sys.executable,
                cwd=Path.cwd(),
                env=dict(os.environ),
                timeout_seconds=0.1,
                max_output_bytes=1024,
            )
        self.assertLess(time.monotonic() - started, 3)

'''
if "test_workspace_read_rejects_in_place_mutation_during_read" not in text:
    text = once(text, marker, addition + marker, "review read/process regressions")
marker = "    def test_approved_request_recovers_issued_authority_before_operation_after_restart(self):\n"
addition = '''    def test_workspace_write_preserves_commit_point_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace, artifacts = self._fixture(root)
            target = workspace / "commit-race.txt"
            target.write_text("initial\\n")
            real_exchange = rooted_io._rename_exchange
            injected = False

            def replace_at_commit(parent_fd: int, left: str, right: str) -> None:
                nonlocal injected
                if not injected:
                    replacement = workspace / "concurrent.txt"
                    replacement.write_text("concurrent\\n")
                    os.replace(replacement, target)
                    injected = True
                real_exchange(parent_fd, left, right)

            with self._open(database, workspace, artifacts) as operator:
                operator.grant(
                    actor_id="a-1",
                    capability_id="workspace.write",
                    resource_scope=("commit-race.txt",),
                )
                with patch(
                    "ai_capital.product.rooted_io._rename_exchange",
                    side_effect=replace_at_commit,
                ):
                    result = operator.invoke(
                        program_id="p-1",
                        actor_id="a-1",
                        capability_id="workspace.write",
                        arguments={"path": "commit-race.txt", "content": "authorized\\n"},
                    )
            self.assertEqual(result["operation"]["execution_outcome"], "failed")
            self.assertEqual(target.read_text(), "concurrent\\n")
            self.assertFalse(any(item.name.endswith(".tmp") for item in workspace.iterdir()))

'''
if "test_workspace_write_preserves_commit_point_replacement" not in text:
    text = once(text, marker, addition + marker, "atomic commit regression")
main_marker = '\n\nif __name__ == "__main__":\n'
windows = '''\n\nclass H2WindowsQualificationTests(unittest.TestCase):
    def test_root_identity_defers_unsupported_rooted_io_until_invocation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(rooted_io.os, "name", "nt"):
                self.assertIsNone(rooted_io.root_identity(root))
'''
if "class H2WindowsQualificationTests" not in text:
    text = once(text, main_marker, windows + main_marker, "Windows startup qualification")
path.write_text(text)

path = Path("tests/test_h2_product_capability_review.py")
text = path.read_text()
text = once(
    text,
    "from ai_capital.product import LocalCapabilityOperator\n",
    "from ai_capital.product import LocalCapabilityOperator\nfrom ai_capital.product.git_repository_guard import validate_git_directory_fd as real_validate_git_directory_fd\n",
    "Git descriptor test import",
)
marker = "    def test_git_observe_rejects_repository_filter_configuration_before_subprocess(self):\n"
addition = '''    @unittest.skipIf(os.name == "nt", "descriptor-backed Git pinning requires rooted descriptors")
    def test_git_observe_pins_git_directory_before_validation_race(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace, artifacts = self._fixture(root)
            self._minimal_git_dir(workspace)
            pinned_git = workspace / ".git-pinned"
            swapped = False

            def validate_then_swap(descriptor: int) -> None:
                nonlocal swapped
                real_validate_git_directory_fd(descriptor)
                if not swapped:
                    (workspace / ".git").rename(pinned_git)
                    replacement = workspace / ".git"
                    replacement.mkdir()
                    (replacement / "config").write_text(
                        "[filter \\\"replacement-driver\\\"]\\n\\tclean = cat\\n"
                    )
                    swapped = True

            completed = SimpleNamespace(returncode=0, stdout="", stderr="")

            def observe(argv, **kwargs):
                git_dir_argument = next(
                    item for item in argv if item.startswith("--git-dir=")
                )
                pinned_path = Path(git_dir_argument.split("=", 1)[1])
                self.assertTrue(os.path.samefile(pinned_path, pinned_git))
                return completed

            with self._open(database, workspace, artifacts) as operator:
                operator.grant(
                    actor_id="a-1",
                    capability_id="git.observe",
                    resource_scope=(".",),
                )
                with patch(
                    "ai_capital.product.capability_executors.validate_git_directory_fd",
                    side_effect=validate_then_swap,
                ), patch(
                    "ai_capital.product.capability_executors.run_bounded_process",
                    side_effect=observe,
                ):
                    result = operator.invoke(
                        program_id="p-1",
                        actor_id="a-1",
                        capability_id="git.observe",
                        arguments={"path": ".", "operation": "status"},
                    )
            self.assertEqual(result["operation"]["execution_outcome"], "succeeded")

'''
if "test_git_observe_pins_git_directory_before_validation_race" not in text:
    text = once(text, marker, addition + marker, "Git pinning regression")
path.write_text(text)
