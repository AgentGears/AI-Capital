from pathlib import Path


def once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected one match, found {count}")
    return text.replace(old, new, 1)


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
new = '''def validate_git_directory(git_dir: Path) -> None:
    try:
        info = os.lstat(git_dir)
    except OSError as exc:
        raise InvalidRequest("git.observe requires a local .git directory") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise InvalidRequest("git.observe rejects indirect Git metadata")

    for forbidden in (
'''
text = once(text, old, new, "Git validator split")
text = text.rstrip() + '''\n\n\ndef validate_git_repository(repository: Path) -> None:\n    validate_git_directory(repository / ".git")\n'''
path.write_text(text)

path = Path("src/ai_capital/product/capability_executors.py")
text = path.read_text()
text = once(
    text,
    "from .git_repository_guard import validate_git_repository\n",
    "from .git_repository_guard import validate_git_directory\n",
    "Git guard import",
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
            validate_git_directory(Path(git_path))
'''
text = once(text, old, new, "pin Git metadata before validation")
path.write_text(text)

path = Path("tests/test_h2_product_capability_review.py")
text = path.read_text()
if "from ai_capital.product.git_repository_guard import validate_git_directory as real_validate_git_directory\n" not in text:
    text = once(
        text,
        "from ai_capital.product import LocalCapabilityOperator\n",
        "from ai_capital.product import LocalCapabilityOperator\nfrom ai_capital.product.git_repository_guard import validate_git_directory as real_validate_git_directory\n",
        "test guard import",
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
                real_validate_git_directory(path)
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
                    "ai_capital.product.capability_executors.validate_git_directory",
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
    text = once(text, marker, test + marker, "Git race regression insertion")
path.write_text(text)
