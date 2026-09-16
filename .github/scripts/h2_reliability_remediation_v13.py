from pathlib import Path

ROOTED = Path("src/ai_capital/product/rooted_io.py")
REVIEW = Path("tests/test_h2_reliability_review.py")

rooted = ROOTED.read_text()

old_signature = '''def validate_pinned(\n    root: Path,\n    target: str,\n    *,\n    root_fd: int,\n    target_fd: int,\n    allow_directory: bool,\n    expected_root_identity: tuple[int, int] | None = None,\n) -> None:\n'''
new_signature = '''def validate_pinned(\n    root: Path,\n    target: str,\n    *,\n    root_fd: int,\n    target_fd: int,\n    allow_directory: bool,\n    expected_root_identity: tuple[int, int] | None = None,\n    expected_parent_fd: int | None = None,\n) -> None:\n'''
if old_signature not in rooted:
    raise SystemExit("validate_pinned signature seam changed")
rooted = rooted.replace(old_signature, new_signature, 1)

old_locals = '''    current_root = _open_root(root, expected_identity=expected_root_identity)\n    parent_fd: int | None = None\n    current_target: int | None = None\n'''
new_locals = '''    current_root = _open_root(root, expected_identity=expected_root_identity)\n    current_parent_fd: int | None = None\n    current_target: int | None = None\n'''
if old_locals not in rooted:
    raise SystemExit("validate_pinned locals seam changed")
rooted = rooted.replace(old_locals, new_locals, 1)

old_parent_open = '''            parent_fd = _open_directory_from(current_root, parts[:-1])\n            flags = (\n                os.O_RDONLY\n                | os.O_NOFOLLOW\n                | getattr(os, "O_NONBLOCK", 0)\n                | getattr(os, "O_BINARY", 0)\n            )\n            current_target = os.open(parts[-1], flags, dir_fd=parent_fd)\n'''
new_parent_open = '''            current_parent_fd = _open_directory_from(current_root, parts[:-1])\n            if expected_parent_fd is not None and not _same_identity(\n                _descriptor_identity(expected_parent_fd),\n                _descriptor_identity(current_parent_fd),\n            ):\n                raise ExecutionFailure("capability parent changed during rooted access")\n            flags = (\n                os.O_RDONLY\n                | os.O_NOFOLLOW\n                | getattr(os, "O_NONBLOCK", 0)\n                | getattr(os, "O_BINARY", 0)\n            )\n            current_target = os.open(parts[-1], flags, dir_fd=current_parent_fd)\n'''
if old_parent_open not in rooted:
    raise SystemExit("validate_pinned parent seam changed")
rooted = rooted.replace(old_parent_open, new_parent_open, 1)

old_finally = '''        if current_target is not None:\n            os.close(current_target)\n        if parent_fd is not None:\n            os.close(parent_fd)\n        os.close(current_root)\n'''
new_finally = '''        if current_target is not None:\n            os.close(current_target)\n        if current_parent_fd is not None:\n            os.close(current_parent_fd)\n        os.close(current_root)\n'''
if old_finally not in rooted:
    raise SystemExit("validate_pinned finally seam changed")
rooted = rooted.replace(old_finally, new_finally, 1)

# Direct rooted operations require both the final component and its parent binding
# to remain current at the final validation point.
for needle in (
    '''            allow_directory=False,\n            expected_root_identity=expected_root_identity,\n        )\n        return content\n''',
    '''                allow_directory=False,\n                expected_root_identity=expected_root_identity,\n            )\n            return\n''',
    '''            allow_directory=False,\n            expected_root_identity=expected_root_identity,\n        )\n    except InvalidRequest:\n''',
    '''            allow_directory=False,\n            expected_root_identity=expected_root_identity,\n        )\n        committed = True\n''',
):
    if needle not in rooted:
        raise SystemExit(f"direct validation seam changed: {needle!r}")
    rooted = rooted.replace(
        needle,
        needle.replace(
            "            expected_root_identity=expected_root_identity,\n",
            "            expected_root_identity=expected_root_identity,\n            expected_parent_fd=parent_fd,\n",
        ).replace(
            "                expected_root_identity=expected_root_identity,\n",
            "                expected_root_identity=expected_root_identity,\n                expected_parent_fd=parent_fd,\n",
        ),
        1,
    )

# The initial open_pinned validation also binds the parent observed during lookup.
open_pinned_call = '''            allow_directory=allow_directory,\n            expected_root_identity=expected_root_identity,\n        )\n        return root_fd, target_fd\n'''
if open_pinned_call not in rooted:
    raise SystemExit("open_pinned validation seam changed")
rooted = rooted.replace(
    open_pinned_call,
    '''            allow_directory=allow_directory,\n            expected_root_identity=expected_root_identity,\n            expected_parent_fd=parent_fd,\n        )\n        return root_fd, target_fd\n''',
    1,
)

# Ensure target='.' cleanup is symmetric if validation fails.
old_root_target = '''    if target == ".":\n        target_fd = os.dup(root_fd)\n        validate_pinned(\n            root,\n            target,\n            root_fd=root_fd,\n            target_fd=target_fd,\n            allow_directory=True,\n            expected_root_identity=expected_root_identity,\n        )\n        return root_fd, target_fd\n'''
new_root_target = '''    if target == ".":\n        target_fd = os.dup(root_fd)\n        try:\n            validate_pinned(\n                root,\n                target,\n                root_fd=root_fd,\n                target_fd=target_fd,\n                allow_directory=True,\n                expected_root_identity=expected_root_identity,\n            )\n        except Exception:\n            os.close(target_fd)\n            os.close(root_fd)\n            raise\n        return root_fd, target_fd\n'''
if old_root_target not in rooted:
    raise SystemExit("root target cleanup seam changed")
rooted = rooted.replace(old_root_target, new_root_target, 1)

# Clean a newly linked workspace target on a detected commit failure, but only if
# the old parent name still identifies the inode created by this invocation.
old_state = '''    descriptor: int | None = None\n    temporary: str | None = None\n    temporary_owned = False\n    try:\n'''
new_state = '''    descriptor: int | None = None\n    temporary: str | None = None\n    temporary_owned = False\n    new_link_created = False\n    new_link_committed = False\n    new_link_identity: tuple[int, int, int] | None = None\n    try:\n'''
if old_state not in rooted:
    raise SystemExit("atomic state seam changed")
rooted = rooted.replace(old_state, new_state, 1)

old_before_link = '''            _validate_parent_binding(\n                root,\n                target,\n                root_fd=root_fd,\n                parent_fd=parent_fd,\n                expected_root_identity=expected_root_identity,\n            )\n            try:\n                os.link(\n'''
new_before_link = '''            _validate_parent_binding(\n                root,\n                target,\n                root_fd=root_fd,\n                parent_fd=parent_fd,\n                expected_root_identity=expected_root_identity,\n            )\n            new_link_identity = _stat_identity(os.fstat(descriptor))\n            try:\n                os.link(\n'''
if old_before_link not in rooted:
    raise SystemExit("atomic pre-link seam changed")
rooted = rooted.replace(old_before_link, new_before_link, 1)

old_after_link = '''            except FileExistsError as exc:\n                raise InvalidRequest(\n                    "capability target changed before rooted materialization commit"\n                ) from exc\n            os.unlink(temporary, dir_fd=parent_fd)\n'''
new_after_link = '''            except FileExistsError as exc:\n                raise InvalidRequest(\n                    "capability target changed before rooted materialization commit"\n                ) from exc\n            new_link_created = True\n            os.unlink(temporary, dir_fd=parent_fd)\n'''
if old_after_link not in rooted:
    raise SystemExit("atomic post-link seam changed")
rooted = rooted.replace(old_after_link, new_after_link, 1)

old_new_return = '''                expected_root_identity=expected_root_identity,\n                expected_parent_fd=parent_fd,\n            )\n            return\n'''
new_new_return = '''                expected_root_identity=expected_root_identity,\n                expected_parent_fd=parent_fd,\n            )\n            new_link_committed = True\n            return\n'''
if old_new_return not in rooted:
    raise SystemExit("atomic new-target commit seam changed")
rooted = rooted.replace(old_new_return, new_new_return, 1)

old_atomic_finally = '''    finally:\n        if descriptor is not None:\n            try:\n                os.close(descriptor)\n            except OSError:\n                pass\n        if temporary_owned and temporary is not None:\n'''
new_atomic_finally = '''    finally:\n        if new_link_created and not new_link_committed and new_link_identity is not None:\n            try:\n                current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)\n                if _stat_identity(current) == new_link_identity:\n                    os.unlink(name, dir_fd=parent_fd)\n                    try:\n                        os.fsync(parent_fd)\n                    except OSError:\n                        pass\n            except OSError:\n                pass\n        if descriptor is not None:\n            try:\n                os.close(descriptor)\n            except OSError:\n                pass\n        if temporary_owned and temporary is not None:\n'''
if old_atomic_finally not in rooted:
    raise SystemExit("atomic cleanup seam changed")
rooted = rooted.replace(old_atomic_finally, new_atomic_finally, 1)

ROOTED.write_text(rooted)

review = REVIEW.read_text()
insert_before = '''    def test_workspace_new_target_replacement_before_final_validation_fails(self):\n'''
regression = r'''    def test_workspace_read_rejects_relinked_inode_under_replaced_parent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace, artifacts = self._fixture(root)
            original_parent = workspace / "dir"
            original_parent.mkdir()
            target = original_parent / "value.txt"
            target.write_text("stable\n")
            pinned_parent = workspace / "pinned-dir"
            real_read = rooted_io._read_bounded
            injected = False

            def read_then_reparent(descriptor: int, *, max_bytes: int) -> bytes:
                nonlocal injected
                content = real_read(descriptor, max_bytes=max_bytes)
                if not injected:
                    original_parent.rename(pinned_parent)
                    original_parent.mkdir()
                    os.link(pinned_parent / "value.txt", original_parent / "value.txt")
                    injected = True
                return content

            with self._open(database, workspace, artifacts) as operator:
                operator.grant(
                    actor_id="a-1",
                    capability_id="workspace.read",
                    resource_scope=("dir/value.txt",),
                )
                with patch(
                    "ai_capital.product.rooted_io._read_bounded",
                    side_effect=read_then_reparent,
                ):
                    result = operator.invoke(
                        program_id="p-1",
                        actor_id="a-1",
                        capability_id="workspace.read",
                        arguments={"path": "dir/value.txt"},
                    )
            self.assertEqual(result["operation"]["execution_outcome"], "failed")
            self.assertEqual(result["operation"]["effect_status"], "not_applicable")
            self.assertTrue(os.path.samefile(
                pinned_parent / "value.txt",
                original_parent / "value.txt",
            ))

'''
if insert_before not in review:
    raise SystemExit("review regression insertion seam changed")
review = review.replace(insert_before, regression + insert_before, 1)
REVIEW.write_text(review)
