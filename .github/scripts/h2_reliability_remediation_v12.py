from pathlib import Path

ROOTED = Path("src/ai_capital/product/rooted_io.py")
EXECUTORS = Path("src/ai_capital/product/capability_executors.py")
HARDENING = Path("tests/test_h2_reliability_hardening.py")
REVIEW = Path("tests/test_h2_reliability_review.py")


def replace_block(text: str, start: str, end: str, replacement: str) -> str:
    begin = text.index(start)
    finish = text.index(end, begin)
    return text[:begin] + replacement.rstrip() + "\n\n" + text[finish:]


rooted = ROOTED.read_text()
helper_marker = "\ndef _write_all(descriptor: int, content: bytes) -> None:\n"
if "def _validate_parent_binding(" not in rooted:
    helpers = r'''
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
) -> None:
    """Require the current rooted name to still identify the pinned target."""
    current_root = _open_root(root, expected_identity=expected_root_identity)
    parent_fd: int | None = None
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
            parent_fd = _open_directory_from(current_root, parts[:-1])
            flags = (
                os.O_RDONLY
                | os.O_NOFOLLOW
                | getattr(os, "O_NONBLOCK", 0)
                | getattr(os, "O_BINARY", 0)
            )
            current_target = os.open(parts[-1], flags, dir_fd=parent_fd)
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
        if parent_fd is not None:
            os.close(parent_fd)
        os.close(current_root)
'''
    rooted = rooted.replace(helper_marker, "\n" + helpers.strip() + "\n\n" + helper_marker.lstrip("\n"), 1)

read_regular = r'''
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
'''
rooted = replace_block(rooted, "def read_regular(\n", "def list_directory(\n", read_regular)

list_directory = r'''
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
'''
rooted = replace_block(rooted, "def list_directory(\n", "def _stat_identity(\n", list_directory)

atomic_write = r'''
def atomic_write(
    root: Path,
    target: str,
    content: bytes,
    *,
    expected_root_identity: tuple[int, int] | None = None,
) -> None:
    """Materialize a rooted write while rejecting stale parent/final bindings."""
    root_fd, parent_fd, name = _open_parent(
        root, target, expected_root_identity=expected_root_identity
    )
    descriptor: int | None = None
    temporary: str | None = None
    temporary_owned = False
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
            validate_pinned(
                root,
                target,
                root_fd=root_fd,
                target_fd=descriptor,
                allow_directory=False,
                expected_root_identity=expected_root_identity,
            )
            return

        expected_identity = _stat_identity(current)
        descriptor = os.open(
            name,
            os.O_RDWR
            | os.O_NOFOLLOW
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_BINARY", 0),
            dir_fd=parent_fd,
        )
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _stat_identity(opened) != expected_identity:
            raise ExecutionFailure("capability target changed before rooted materialization")

        before_write = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            stat.S_ISLNK(before_write.st_mode)
            or not stat.S_ISREG(before_write.st_mode)
            or _stat_identity(before_write) != expected_identity
        ):
            raise ExecutionFailure("capability target changed before rooted materialization")
        _validate_parent_binding(
            root,
            target,
            root_fd=root_fd,
            parent_fd=parent_fd,
            expected_root_identity=expected_root_identity,
        )

        # Mutation stays confined to the authorized inode. If its rooted name changes
        # after this boundary, final validation rejects success rather than touching
        # the replacement path.
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        _write_all(descriptor, content)
        os.fchmod(descriptor, _PRIVATE_FILE_MODE)
        os.fsync(descriptor)

        os.lseek(descriptor, 0, os.SEEK_SET)
        materialized = _read_bounded(descriptor, max_bytes=len(content))
        verified = os.fstat(descriptor)
        committed = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        same_object = (
            verified.st_dev == opened.st_dev
            and verified.st_ino == opened.st_ino
            and committed.st_dev == verified.st_dev
            and committed.st_ino == verified.st_ino
        )
        stable_commit = (
            same_object
            and stat.S_ISREG(verified.st_mode)
            and stat.S_ISREG(committed.st_mode)
            and materialized == content
            and verified.st_size == len(content)
            and committed.st_size == verified.st_size
            and committed.st_mtime_ns == verified.st_mtime_ns
            and committed.st_ctime_ns == verified.st_ctime_ns
            and stat.S_IMODE(verified.st_mode) == _PRIVATE_FILE_MODE
            and stat.S_IMODE(committed.st_mode) == _PRIVATE_FILE_MODE
        )
        if not stable_commit:
            raise ExecutionFailure(
                "capability target changed during rooted materialization commit"
            )
        os.fsync(parent_fd)
        validate_pinned(
            root,
            target,
            root_fd=root_fd,
            target_fd=descriptor,
            allow_directory=False,
            expected_root_identity=expected_root_identity,
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
        if temporary_owned and temporary is not None:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except OSError:
                pass
        os.close(parent_fd)
        os.close(root_fd)
'''
rooted = replace_block(rooted, "def atomic_write(\n", "def exclusive_create(\n", atomic_write)

exclusive_create = r'''
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
'''
rooted = replace_block(rooted, "def exclusive_create(\n", "def open_pinned(\n", exclusive_create)

open_pinned = r'''
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
        validate_pinned(
            root,
            target,
            root_fd=root_fd,
            target_fd=target_fd,
            allow_directory=True,
            expected_root_identity=expected_root_identity,
        )
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
'''
rooted = replace_block(rooted, "def open_pinned(\n", "def descriptor_path(\n", open_pinned)
ROOTED.write_text(rooted)

executors = EXECUTORS.read_text()
old_import = "    open_pinned,\n    read_regular,\n)"
new_import = "    open_pinned,\n    read_regular,\n    validate_pinned,\n)"
if old_import not in executors:
    raise SystemExit("rooted import seam changed")
executors = executors.replace(old_import, new_import, 1)
old_command = '''            completed = run_bounded_process(\n                argv,\n                cwd=cwd,\n                executable=self._trusted_executable(command),\n                env=self._process_environment(),\n                timeout_seconds=_COMMAND_TIMEOUT_SECONDS,\n                max_output_bytes=_MAX_OBSERVATION_BYTES,\n                pass_fds=(root_fd, target_fd),\n            )\n        finally:\n'''
new_command = '''            completed = run_bounded_process(\n                argv,\n                cwd=cwd,\n                executable=self._trusted_executable(command),\n                env=self._process_environment(),\n                timeout_seconds=_COMMAND_TIMEOUT_SECONDS,\n                max_output_bytes=_MAX_OBSERVATION_BYTES,\n                pass_fds=(root_fd, target_fd),\n            )\n            validate_pinned(\n                self._workspace_root,\n                operand,\n                root_fd=root_fd,\n                target_fd=target_fd,\n                allow_directory=allow_directory,\n                expected_root_identity=self._workspace_root_identity,\n            )\n        finally:\n'''
if old_command not in executors:
    raise SystemExit("command validation seam changed")
executors = executors.replace(old_command, new_command, 1)
old_git = '''                completed = run_bounded_process(\n                    argv,\n                    cwd=repository_path,\n                    executable=executable,\n                    env=environment,\n                    timeout_seconds=_COMMAND_TIMEOUT_SECONDS,\n                    max_output_bytes=_MAX_OBSERVATION_BYTES,\n                    pass_fds=(root_fd, repository_fd, git_fd),\n                )\n                validate_git_directory_fd(git_fd)\n        finally:\n'''
new_git = '''                completed = run_bounded_process(\n                    argv,\n                    cwd=repository_path,\n                    executable=executable,\n                    env=environment,\n                    timeout_seconds=_COMMAND_TIMEOUT_SECONDS,\n                    max_output_bytes=_MAX_OBSERVATION_BYTES,\n                    pass_fds=(root_fd, repository_fd, git_fd),\n                )\n                validate_git_directory_fd(git_fd)\n                validate_pinned(\n                    self._workspace_root,\n                    target,\n                    root_fd=root_fd,\n                    target_fd=repository_fd,\n                    allow_directory=True,\n                    expected_root_identity=self._workspace_root_identity,\n                )\n        finally:\n'''
if old_git not in executors:
    raise SystemExit("git validation seam changed")
executors = executors.replace(old_git, new_git, 1)
EXECUTORS.write_text(executors)

hardening = HARDENING.read_text()
old_parent_assertions = '''            self.assertEqual(result["operation"]["execution_outcome"], "succeeded")\n            self.assertFalse((outside / "value.txt").exists())\n            self.assertEqual((pinned / "value.txt").read_text(), "pinned\\n")\n'''
new_parent_assertions = '''            self.assertEqual(result["operation"]["execution_outcome"], "failed")\n            self.assertFalse((outside / "value.txt").exists())\n            self.assertFalse((pinned / "value.txt").exists())\n'''
if old_parent_assertions not in hardening:
    raise SystemExit("parent swap assertion seam changed")
hardening = hardening.replace(old_parent_assertions, new_parent_assertions, 1)
HARDENING.write_text(hardening)

review = REVIEW.read_text()
insert_before = '''    def test_workspace_write_rejects_final_component_replacement_before_commit(self):\n'''
new_tests = r'''    def test_workspace_read_rejects_parent_replacement_after_pin(self):
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace, artifacts = self._fixture(root)
            original_parent = workspace / "dir"
            original_parent.mkdir()
            (original_parent / "value.txt").write_text("stable\n")
            pinned = workspace / "pinned-dir"
            outside = root / "outside"
            outside.mkdir()
            real_open_parent = rooted_io._open_parent
            swapped = False

            def swap_after_pin(base: Path, target: str, **kwargs):
                nonlocal swapped
                result = real_open_parent(base, target, **kwargs)
                if not swapped:
                    original_parent.rename(pinned)
                    original_parent.symlink_to(outside, target_is_directory=True)
                    swapped = True
                return result

            with self._open(database, workspace, artifacts) as operator:
                operator.grant(
                    actor_id="a-1",
                    capability_id="workspace.read",
                    resource_scope=("dir/value.txt",),
                )
                with patch(
                    "ai_capital.product.rooted_io._open_parent",
                    side_effect=swap_after_pin,
                ):
                    result = operator.invoke(
                        program_id="p-1",
                        actor_id="a-1",
                        capability_id="workspace.read",
                        arguments={"path": "dir/value.txt"},
                    )
            self.assertEqual(result["operation"]["execution_outcome"], "failed")
            self.assertEqual(result["operation"]["effect_status"], "not_applicable")

    def test_workspace_new_target_replacement_before_final_validation_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace, artifacts = self._fixture(root)
            target = workspace / "new-race.txt"
            original_validate = rooted_io.validate_pinned
            injected = False

            def replace_before_validation(*args, **kwargs):
                nonlocal injected
                if not injected and kwargs.get("target_fd") is not None:
                    replacement = workspace / "replacement.txt"
                    replacement.write_text("concurrent\n")
                    os.replace(replacement, target)
                    injected = True
                return original_validate(*args, **kwargs)

            with self._open(database, workspace, artifacts) as operator:
                operator.grant(
                    actor_id="a-1",
                    capability_id="workspace.write",
                    resource_scope=("new-race.txt",),
                )
                with patch(
                    "ai_capital.product.rooted_io.validate_pinned",
                    side_effect=replace_before_validation,
                ):
                    result = operator.invoke(
                        program_id="p-1",
                        actor_id="a-1",
                        capability_id="workspace.write",
                        arguments={"path": "new-race.txt", "content": "authorized\n"},
                    )
            self.assertEqual(result["operation"]["execution_outcome"], "failed")
            self.assertEqual(result["operation"]["effect_status"], "indeterminate")
            self.assertEqual(target.read_text(), "concurrent\n")
            self.assertFalse(any(item.name.endswith(".tmp") for item in workspace.iterdir()))

    def test_artifact_create_replacement_before_final_validation_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace, artifacts = self._fixture(root)
            target = artifacts / "artifact-race.txt"
            original_validate = rooted_io.validate_pinned
            injected = False

            def replace_before_validation(*args, **kwargs):
                nonlocal injected
                if not injected and kwargs.get("target_fd") is not None:
                    replacement = artifacts / "replacement.txt"
                    replacement.write_text("concurrent\n")
                    os.replace(replacement, target)
                    injected = True
                return original_validate(*args, **kwargs)

            with self._open(database, workspace, artifacts) as operator:
                operator.grant(
                    actor_id="a-1",
                    capability_id="artifact.write",
                    resource_scope=("artifact-race.txt",),
                )
                with patch(
                    "ai_capital.product.rooted_io.validate_pinned",
                    side_effect=replace_before_validation,
                ):
                    result = operator.invoke(
                        program_id="p-1",
                        actor_id="a-1",
                        capability_id="artifact.write",
                        arguments={"path": "artifact-race.txt", "content": "authorized\n"},
                    )
            self.assertEqual(result["operation"]["execution_outcome"], "failed")
            self.assertEqual(result["operation"]["effect_status"], "indeterminate")
            self.assertEqual(target.read_text(), "concurrent\n")

'''
if insert_before not in review:
    raise SystemExit("review insertion seam changed")
review = review.replace(insert_before, new_tests + insert_before, 1)
REVIEW.write_text(review)
