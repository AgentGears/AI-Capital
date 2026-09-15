from pathlib import Path


def once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected one match, found {count}")
    return text.replace(old, new, 1)


# 1. Process-group termination must escalate descendants even if the leader exits.
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

    if process.poll() is not None and not _process_group_exists(process.pid):
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass
    deadline = time.monotonic() + _TERMINATION_GRACE_SECONDS
    try:
        process.wait(timeout=_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    remaining = deadline - time.monotonic()
    if remaining > 0:
        time.sleep(remaining)
    if _process_group_exists(process.pid):
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
    if process.poll() is None:
        try:
            process.wait(timeout=_TERMINATION_GRACE_SECONDS)
        except subprocess.TimeoutExpired as exc:
            raise ExecutionFailure("observation subprocess could not be terminated") from exc
'''
text = once(text, old, new, "process group escalation")
path.write_text(text)


# 2. Preserve stable-read mutation detection in the rooted reader.
path = Path("src/ai_capital/product/rooted_io.py")
text = path.read_text()
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
        if not stat.S_ISREG(before.st_mode):
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
            not stat.S_ISREG(after.st_mode)
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
text = once(text, old, new, "rooted stable read")
old = '''def root_identity(root: Path) -> tuple[int, int]:
    descriptor = _open_root(root)
'''
new = '''def root_identity(root: Path) -> tuple[int, int] | None:
    if os.name == "nt":
        return None
    descriptor = _open_root(root)
'''
text = once(text, old, new, "Windows deferred root identity")
path.write_text(text)


# 3. Recover interrupted Operations when a product writer is reopened.
path = Path("src/ai_capital/product/capability_operator.py")
text = path.read_text()
old = '''        self._controls = ProgramControlRepository(programs)
        self._journal = OperationJournal(programs)
        self._host = OperationHost(self._journal, self._authority)
'''
new = '''        self._controls = ProgramControlRepository(programs)
        self._journal = OperationJournal(programs)
        if owns_repository:
            self._journal.recover_interrupted()
        self._host = OperationHost(self._journal, self._authority)
'''
text = once(text, old, new, "capability startup recovery")
path.write_text(text)

path = Path("src/ai_capital/product/program_operator.py")
text = path.read_text()
old = '''        self._controls = ProgramControlRepository(programs)
        self._operations = OperationJournal(programs)
        self._audit: LocalAuditOperator | None = None
'''
new = '''        self._controls = ProgramControlRepository(programs)
        self._operations = OperationJournal(programs)
        if owns_repository:
            self._operations.recover_interrupted()
        self._audit: LocalAuditOperator | None = None
'''
text = once(text, old, new, "program startup recovery")
path.write_text(text)


# 4. Pin the .git directory before validating its contents.
path = Path("src/ai_capital/product/git_repository_guard.py")
text = path.read_text()
old = '''def validate_git_repository(repository: Path) -> None:
    git_dir = repository / ".git"
    try:
        info = os.lstat(git_dir)
    except OSError as exc:
        raise InvalidRequest("git.observe requires a local .git directory") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise InvalidRequest("git.observe rejects indirect Git metadata")

    for forbidden in (
'''
new = '''def _validate_git_directory_contents(git_dir: Path) -> None:
    for forbidden in (
'''
text = once(text, old, new, "Git guard content split")
text = text.rstrip() + '''\n\n\ndef validate_pinned_git_directory(git_dir: Path) -> None:\n    try:\n        info = os.stat(git_dir)\n    except OSError as exc:\n        raise InvalidRequest("git.observe requires stable local Git metadata") from exc\n    if not stat.S_ISDIR(info.st_mode):\n        raise InvalidRequest("git.observe requires stable local Git metadata")\n    _validate_git_directory_contents(git_dir)\n\n\ndef validate_git_repository(repository: Path) -> None:\n    git_dir = repository / ".git"\n    try:\n        info = os.lstat(git_dir)\n    except OSError as exc:\n        raise InvalidRequest("git.observe requires a local .git directory") from exc\n    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):\n        raise InvalidRequest("git.observe rejects indirect Git metadata")\n    _validate_git_directory_contents(git_dir)\n'''
path.write_text(text)

path = Path("src/ai_capital/product/capability_executors.py")
text = path.read_text()
text = once(
    text,
    "from .git_repository_guard import validate_git_repository\n",
    "from .git_repository_guard import validate_pinned_git_directory\n",
    "pinned Git guard import",
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
            git_path = descriptor_path(git_fd)
            validate_pinned_git_directory(Path(git_path))
'''
text = once(text, old, new, "pin Git directory before validation")
path.write_text(text)


# 5. Regressions.
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
text = once(text, old, new, "startup recovery regression")
path.write_text(text)

path = Path("tests/test_h2_reliability_review.py")
text = path.read_text()
if "from ai_capital.product import process_observation\n" not in text:
    text = once(
        text,
        "from ai_capital.product import rooted_io\n",
        "from ai_capital.product import rooted_io\nfrom ai_capital.product import process_observation\n",
        "process observation test import",
    )
marker = "    def test_workspace_root_swap_to_symlink_fails_closed(self):\n"
tests = '''    def test_workspace_read_rejects_in_place_mutation_during_read(self):
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

    def test_process_group_termination_escalates_if_leader_exits_first(self):
        class FinishedLeader:
            pid = 424242

            def poll(self):
                return None

            def wait(self, timeout):
                return 0

        with patch.object(
            process_observation,
            "_process_group_exists",
            return_value=True,
        ), patch.object(process_observation.os, "killpg") as killpg, patch.object(
            process_observation.time,
            "sleep",
        ):
            process_observation._terminate_process_group(FinishedLeader())
        signals = [call.args[1] for call in killpg.call_args_list]
        self.assertIn(process_observation.signal.SIGTERM, signals)
        self.assertIn(process_observation.signal.SIGKILL, signals)

'''
if "test_workspace_read_rejects_in_place_mutation_during_read" not in text:
    text = once(text, marker, tests + marker, "reliability review regressions")
# Add a non-skipped Windows qualification test after the POSIX class and before main.
main_marker = '\n\nif __name__ == "__main__":\n'
windows_test = '''\n\nclass H2WindowsQualificationTests(unittest.TestCase):
    def test_root_identity_defers_unsupported_windows_rooted_io(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(rooted_io.os, "name", "nt"):
                self.assertIsNone(rooted_io.root_identity(root))
'''
if "class H2WindowsQualificationTests" not in text:
    text = once(text, main_marker, windows_test + main_marker, "Windows qualification regression")
path.write_text(text)

path = Path("tests/test_h2_product_capability_review.py")
text = path.read_text()
if "validate_pinned_git_directory as real_validate_pinned_git_directory" not in text:
    text = once(
        text,
        "from ai_capital.product import LocalCapabilityOperator\n",
        "from ai_capital.product import LocalCapabilityOperator\nfrom ai_capital.product.git_repository_guard import validate_pinned_git_directory as real_validate_pinned_git_directory\n",
        "pinned Git test import",
    )
marker = "    def test_git_observe_rejects_repository_filter_configuration_before_subprocess(self):\n"
test = '''    @unittest.skipIf(os.name == "nt", "descriptor-backed Git pinning requires POSIX")
    def test_git_observe_pins_git_directory_before_validation_race(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace, artifacts = self._fixture(root)
            self._minimal_git_dir(workspace)
            pinned_git = workspace / ".git-pinned"
            swapped = False

            def validate_then_swap(path: Path) -> None:
                nonlocal swapped
                real_validate_pinned_git_directory(path)
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
                    "ai_capital.product.capability_executors.validate_pinned_git_directory",
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
            self.assertTrue(pinned_git.is_dir())

'''
if "test_git_observe_pins_git_directory_before_validation_race" not in text:
    text = once(text, marker, test + marker, "pinned Git race regression")
path.write_text(text)
