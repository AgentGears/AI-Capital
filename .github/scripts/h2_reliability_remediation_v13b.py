from pathlib import Path

ROOTED = Path("src/ai_capital/product/rooted_io.py")
REVIEW = Path("tests/test_h2_reliability_review.py")


def split_function(text: str, start: str, end: str) -> tuple[str, str, str]:
    begin = text.index(start)
    finish = text.index(end, begin)
    return text[:begin], text[begin:finish], text[finish:]


rooted = ROOTED.read_text()

old = '''def validate_pinned(\n    root: Path,\n    target: str,\n    *,\n    root_fd: int,\n    target_fd: int,\n    allow_directory: bool,\n    expected_root_identity: tuple[int, int] | None = None,\n) -> None:\n'''
new = '''def validate_pinned(\n    root: Path,\n    target: str,\n    *,\n    root_fd: int,\n    target_fd: int,\n    allow_directory: bool,\n    expected_root_identity: tuple[int, int] | None = None,\n    expected_parent_fd: int | None = None,\n) -> None:\n'''
if rooted.count(old) != 1:
    raise SystemExit("validate signature seam changed")
rooted = rooted.replace(old, new, 1)

old = '''    current_root = _open_root(root, expected_identity=expected_root_identity)\n    parent_fd: int | None = None\n    current_target: int | None = None\n'''
new = '''    current_root = _open_root(root, expected_identity=expected_root_identity)\n    current_parent_fd: int | None = None\n    current_target: int | None = None\n'''
if rooted.count(old) != 1:
    raise SystemExit("validate local seam changed")
rooted = rooted.replace(old, new, 1)

old = '''            parent_fd = _open_directory_from(current_root, parts[:-1])\n            flags = (\n                os.O_RDONLY\n                | os.O_NOFOLLOW\n                | getattr(os, "O_NONBLOCK", 0)\n                | getattr(os, "O_BINARY", 0)\n            )\n            current_target = os.open(parts[-1], flags, dir_fd=parent_fd)\n'''
new = '''            current_parent_fd = _open_directory_from(current_root, parts[:-1])\n            if expected_parent_fd is not None and not _same_identity(\n                _descriptor_identity(expected_parent_fd),\n                _descriptor_identity(current_parent_fd),\n            ):\n                raise ExecutionFailure("capability parent changed during rooted access")\n            flags = (\n                os.O_RDONLY\n                | os.O_NOFOLLOW\n                | getattr(os, "O_NONBLOCK", 0)\n                | getattr(os, "O_BINARY", 0)\n            )\n            current_target = os.open(parts[-1], flags, dir_fd=current_parent_fd)\n'''
if rooted.count(old) != 1:
    raise SystemExit("validate parent seam changed")
rooted = rooted.replace(old, new, 1)

old = '''        if current_target is not None:\n            os.close(current_target)\n        if parent_fd is not None:\n            os.close(parent_fd)\n        os.close(current_root)\n'''
new = '''        if current_target is not None:\n            os.close(current_target)\n        if current_parent_fd is not None:\n            os.close(current_parent_fd)\n        os.close(current_root)\n'''
if rooted.count(old) != 1:
    raise SystemExit("validate cleanup seam changed")
rooted = rooted.replace(old, new, 1)

# read_regular: final validation must include the parent descriptor.
prefix, block, suffix = split_function(rooted, "def read_regular(\n", "def list_directory(\n")
old = '''            allow_directory=False,\n            expected_root_identity=expected_root_identity,\n        )\n'''
new = '''            allow_directory=False,\n            expected_root_identity=expected_root_identity,\n            expected_parent_fd=parent_fd,\n        )\n'''
if block.count(old) != 1:
    raise SystemExit("read validation seam changed")
block = block.replace(old, new, 1)
rooted = prefix + block + suffix

# atomic_write: validate both parent and final target together, and safely clean a
# newly linked target if final validation fails.
prefix, block, suffix = split_function(rooted, "def atomic_write(\n", "def exclusive_create(\n")
old = '''    descriptor: int | None = None\n    temporary: str | None = None\n    temporary_owned = False\n    try:\n'''
new = '''    descriptor: int | None = None\n    temporary: str | None = None\n    temporary_owned = False\n    new_link_created = False\n    new_link_committed = False\n    new_link_identity: tuple[int, int, int] | None = None\n    try:\n'''
if block.count(old) != 1:
    raise SystemExit("atomic state seam changed")
block = block.replace(old, new, 1)
old = '''            )\n            try:\n                os.link(\n                    temporary,\n'''
new = '''            )\n            new_link_identity = _stat_identity(os.fstat(descriptor))\n            try:\n                os.link(\n                    temporary,\n'''
if block.count(old) != 1:
    raise SystemExit("atomic pre-link seam changed")
block = block.replace(old, new, 1)
old = '''            except FileExistsError as exc:\n                raise InvalidRequest(\n                    "capability target changed before rooted materialization commit"\n                ) from exc\n            os.unlink(temporary, dir_fd=parent_fd)\n'''
new = '''            except FileExistsError as exc:\n                raise InvalidRequest(\n                    "capability target changed before rooted materialization commit"\n                ) from exc\n            new_link_created = True\n            os.unlink(temporary, dir_fd=parent_fd)\n'''
if block.count(old) != 1:
    raise SystemExit("atomic link seam changed")
block = block.replace(old, new, 1)
old = '''                allow_directory=False,\n                expected_root_identity=expected_root_identity,\n            )\n'''
new = '''                allow_directory=False,\n                expected_root_identity=expected_root_identity,\n                expected_parent_fd=parent_fd,\n            )\n'''
if block.count(old) != 1:
    raise SystemExit(f"atomic new validation seam changed: {block.count(old)}")
block = block.replace(old, new, 1)
old = '''            )\n            return\n\n        expected_identity = _stat_identity(current)\n'''
new = '''            )\n            new_link_committed = True\n            return\n\n        expected_identity = _stat_identity(current)\n'''
if block.count(old) != 1:
    raise SystemExit("atomic new commit seam changed")
block = block.replace(old, new, 1)
old = '''            allow_directory=False,\n            expected_root_identity=expected_root_identity,\n        )\n'''
new = '''            allow_directory=False,\n            expected_root_identity=expected_root_identity,\n            expected_parent_fd=parent_fd,\n        )\n'''
if block.count(old) != 1:
    raise SystemExit(f"atomic existing validation seam changed: {block.count(old)}")
block = block.replace(old, new, 1)
old = '''    finally:\n        if descriptor is not None:\n            try:\n                os.close(descriptor)\n            except OSError:\n                pass\n        if temporary_owned and temporary is not None:\n'''
new = '''    finally:\n        if new_link_created and not new_link_committed and new_link_identity is not None:\n            try:\n                current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)\n                if _stat_identity(current) == new_link_identity:\n                    os.unlink(name, dir_fd=parent_fd)\n                    try:\n                        os.fsync(parent_fd)\n                    except OSError:\n                        pass\n            except OSError:\n                pass\n        if descriptor is not None:\n            try:\n                os.close(descriptor)\n            except OSError:\n                pass\n        if temporary_owned and temporary is not None:\n'''
if block.count(old) != 1:
    raise SystemExit("atomic cleanup seam changed")
block = block.replace(old, new, 1)
rooted = prefix + block + suffix

# artifact create: final validation includes the parent identity too.
prefix, block, suffix = split_function(rooted, "def exclusive_create(\n", "def open_pinned(\n")
old = '''            allow_directory=False,\n            expected_root_identity=expected_root_identity,\n        )\n'''
new = '''            allow_directory=False,\n            expected_root_identity=expected_root_identity,\n            expected_parent_fd=parent_fd,\n        )\n'''
if block.count(old) != 1:
    raise SystemExit("exclusive validation seam changed")
block = block.replace(old, new, 1)
rooted = prefix + block + suffix

# open_pinned: bind the parent at initial lookup and clean descriptors on a root-target
# validation failure.
prefix, block, suffix = split_function(rooted, "def open_pinned(\n", "def descriptor_path(")
old = '''    if target == ".":\n        target_fd = os.dup(root_fd)\n        validate_pinned(\n            root,\n            target,\n            root_fd=root_fd,\n            target_fd=target_fd,\n            allow_directory=True,\n            expected_root_identity=expected_root_identity,\n        )\n        return root_fd, target_fd\n'''
new = '''    if target == ".":\n        target_fd = os.dup(root_fd)\n        try:\n            validate_pinned(\n                root,\n                target,\n                root_fd=root_fd,\n                target_fd=target_fd,\n                allow_directory=True,\n                expected_root_identity=expected_root_identity,\n            )\n        except Exception:\n            os.close(target_fd)\n            os.close(root_fd)\n            raise\n        return root_fd, target_fd\n'''
if block.count(old) != 1:
    raise SystemExit("open root seam changed")
block = block.replace(old, new, 1)
old = '''            allow_directory=allow_directory,\n            expected_root_identity=expected_root_identity,\n        )\n'''
new = '''            allow_directory=allow_directory,\n            expected_root_identity=expected_root_identity,\n            expected_parent_fd=parent_fd,\n        )\n'''
if block.count(old) != 1:
    raise SystemExit("open validation seam changed")
block = block.replace(old, new, 1)
rooted = prefix + block + suffix

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
            self.assertTrue(
                os.path.samefile(
                    pinned_parent / "value.txt",
                    original_parent / "value.txt",
                )
            )

'''
if review.count(insert_before) != 1:
    raise SystemExit("review insertion seam changed")
review = review.replace(insert_before, regression + insert_before, 1)
REVIEW.write_text(review)
