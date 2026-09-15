from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected one match, found {count}")
    return text.replace(old, new, 1)


# Replace exchange/rollback materialization with descriptor-confined existing-target writes.
path = Path("src/ai_capital/product/rooted_io.py")
text = path.read_text()
text = replace_once(text, "import ctypes\n", "", "remove ctypes")
start = text.index("def _rename_exchange(")
end = text.index("\ndef exclusive_create(", start)
replacement = '''def atomic_write(
    root: Path,
    target: str,
    content: bytes,
    *,
    expected_root_identity: tuple[int, int] | None = None,
) -> None:
    """Materialize a rooted write without ever replacing an unvalidated concurrent target.

    New targets commit with an atomic no-overwrite hard link. Existing targets are
    pinned by descriptor and updated through that descriptor; a concurrent path
    replacement therefore remains untouched and causes the Operation to fail
    indeterminate at final verification rather than being exchanged or rolled back.
    """
    root_fd, parent_fd, name = _open_parent(
        root, target, expected_root_identity=expected_root_identity
    )
    descriptor: int | None = None
    temporary: str | None = None
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
            os.close(descriptor)
            descriptor = None
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

        # From this point an interruption can leave an effect on the pinned inode;
        # OperationHost therefore records any exception as indeterminate. Crucially,
        # a later path replacement is never opened, renamed, unlinked, or overwritten.
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
text = text[:start] + replacement + text[end + 1 :]
path.write_text(text)


# Replace the exchange-specific regression with one proving the latest concurrent
# replacement remains at the path and no rollback/temp mutation occurs.
path = Path("tests/test_h2_reliability_review.py")
text = path.read_text()
start = text.index("    def test_workspace_write_preserves_commit_point_replacement(self):")
end = text.index(
    "    def test_approved_request_recovers_issued_authority_before_operation_after_restart",
    start,
)
replacement = '''    def test_workspace_write_preserves_latest_replacement_without_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace, artifacts = self._fixture(root)
            target = workspace / "commit-race.txt"
            target.write_text("initial\\n")
            original_write_all = rooted_io._write_all
            injected = False

            def replace_path_twice(descriptor: int, content: bytes) -> None:
                nonlocal injected
                original_write_all(descriptor, content)
                if injected:
                    return
                first = workspace / "concurrent-one.txt"
                first.write_text("concurrent-one\\n")
                os.replace(first, target)
                second = workspace / "concurrent-two.txt"
                second.write_text("concurrent-two\\n")
                os.replace(second, target)
                injected = True

            with self._open(database, workspace, artifacts) as operator:
                operator.grant(
                    actor_id="a-1",
                    capability_id="workspace.write",
                    resource_scope=("commit-race.txt",),
                )
                with patch(
                    "ai_capital.product.rooted_io._write_all",
                    side_effect=replace_path_twice,
                ):
                    result = operator.invoke(
                        program_id="p-1",
                        actor_id="a-1",
                        capability_id="workspace.write",
                        arguments={"path": "commit-race.txt", "content": "authorized\\n"},
                    )
            self.assertEqual(result["operation"]["execution_outcome"], "failed")
            self.assertEqual(result["operation"]["effect_status"], "indeterminate")
            self.assertEqual(target.read_text(), "concurrent-two\\n")
            self.assertFalse(any(item.name.endswith(".tmp") for item in workspace.iterdir()))

'''
text = text[:start] + replacement + text[end:]
path.write_text(text)
