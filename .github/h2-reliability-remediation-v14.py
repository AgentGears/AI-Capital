from pathlib import Path

SOURCE = Path("src/ai_capital/product/rooted_io.py")
TESTS = Path("tests/test_h2_reliability_review.py")

source = SOURCE.read_text()
start = source.index("\ndef atomic_write(\n")
end = source.index("\ndef exclusive_create(\n", start)

replacement = r'''
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
'''

SOURCE.write_text(source[:start] + replacement + source[end:])

tests = TESTS.read_text()
anchor = "    def test_workspace_write_rejects_final_component_replacement_before_commit(self):\n"
addition = '''    def test_workspace_write_detaches_alias_created_at_commit_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace, artifacts = self._fixture(root)
            target = workspace / "alias-race.txt"
            target.write_text("initial\\n")
            outside = root / "outside-race.txt"
            real_claim = rooted_io._claim_existing_target
            injected = False

            def link_then_claim(parent_fd: int, name: str, displaced: str) -> None:
                nonlocal injected
                if not injected:
                    os.link(target, outside)
                    injected = True
                real_claim(parent_fd, name, displaced)

            with self._open(database, workspace, artifacts) as operator:
                operator.grant(
                    actor_id="a-1",
                    capability_id="workspace.write",
                    resource_scope=("alias-race.txt",),
                )
                with patch(
                    "ai_capital.product.rooted_io._claim_existing_target",
                    side_effect=link_then_claim,
                ):
                    result = operator.invoke(
                        program_id="p-1",
                        actor_id="a-1",
                        capability_id="workspace.write",
                        arguments={"path": "alias-race.txt", "content": "authorized\\n"},
                    )

            self.assertTrue(injected)
            self.assertEqual(result["operation"]["execution_outcome"], "succeeded")
            self.assertEqual(result["operation"]["effect_status"], "confirmed")
            self.assertEqual(target.read_text(), "authorized\\n")
            self.assertEqual(outside.read_text(), "initial\\n")
            self.assertFalse(os.path.samefile(outside, target))

'''
if anchor not in tests:
    raise SystemExit("test insertion anchor not found")
tests = tests.replace(anchor, addition + anchor, 1)
TESTS.write_text(tests)
