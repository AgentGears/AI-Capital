from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from ai_capital.kernel.actor_store import ActorRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ProgramStatus
from ai_capital.kernel.errors import InvalidRequest
from ai_capital.kernel.models import Actor, Program
from ai_capital.product import LocalCapabilityOperator


class H2ProductCapabilityReviewTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, Path, Path]:
        database = root / "capital.db"
        workspace = root / "workspace"
        artifacts = root / "generated-artifacts"
        workspace.mkdir()
        artifacts.mkdir()
        with ProgramRepository(database) as programs:
            programs.create(Program("p-1", 0, "H2.6 review remediation"))
            programs.transition("p-1", ProgramStatus.ACTIVE, expected_revision=0)
            ActorRepository(programs).register(
                Actor("a-1", 0, "worker", "configured-binding")
            )
        return database, workspace, artifacts

    def _open(self, database: Path, workspace: Path, artifacts: Path):
        return LocalCapabilityOperator.open(
            database,
            workspace_root=workspace,
            artifact_root=artifacts,
        )

    @staticmethod
    def _minimal_git_dir(repository: Path) -> Path:
        git_dir = repository / ".git"
        git_dir.mkdir(parents=True)
        (git_dir / "config").write_text(
            "[core]\n"
            "\trepositoryformatversion = 0\n"
            "\tbare = false\n"
        )
        return git_dir

    def test_create_only_artifact_does_not_replace_existing_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace, artifacts = self._fixture(root)
            target = artifacts / "report.txt"
            target.write_text("original\n")
            with self._open(database, workspace, artifacts) as operator:
                operator.grant(
                    actor_id="a-1",
                    capability_id="artifact.write",
                    resource_scope=("report.txt",),
                )
                result = operator.invoke(
                    program_id="p-1",
                    actor_id="a-1",
                    capability_id="artifact.write",
                    arguments={"path": "report.txt", "content": "replacement\n"},
                )
            self.assertEqual(result["operation"]["execution_outcome"], "failed")
            self.assertEqual(target.read_text(), "original\n")

    def test_git_observe_uses_fixed_arguments_and_isolated_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace, artifacts = self._fixture(root)
            self._minimal_git_dir(workspace)
            completed = SimpleNamespace(returncode=0, stdout="", stderr="")
            with patch.dict(os.environ, {"GIT_DIR": str(root / "outside")}, clear=False):
                with self._open(database, workspace, artifacts) as operator:
                    operator.grant(
                        actor_id="a-1",
                        capability_id="git.observe",
                        resource_scope=(".",),
                    )
                    with patch(
                        "ai_capital.product.capability_executors.subprocess.run",
                        return_value=completed,
                    ) as run:
                        result = operator.invoke(
                            program_id="p-1",
                            actor_id="a-1",
                            capability_id="git.observe",
                            arguments={"path": ".", "operation": "diff"},
                        )
            self.assertEqual(result["operation"]["execution_outcome"], "succeeded")
            argv = run.call_args.args[0]
            self.assertEqual(argv[0], "git")
            self.assertIn("core.fsmonitor=false", argv)
            self.assertIn("log.showSignature=false", argv)
            self.assertIn("submodule.recurse=false", argv)
            self.assertIn("--no-ext-diff", argv)
            self.assertIn("--no-textconv", argv)
            self.assertIn("--ignore-submodules=all", argv)
            self.assertFalse(run.call_args.kwargs["shell"])
            environment = run.call_args.kwargs["env"]
            self.assertNotIn("GIT_DIR", environment)
            self.assertEqual(environment["GIT_CONFIG_NOSYSTEM"], "1")
            self.assertEqual(environment["HOME"], environment["XDG_CONFIG_HOME"])

    def test_git_observe_rejects_repository_filter_configuration_before_subprocess(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace, artifacts = self._fixture(root)
            git_dir = self._minimal_git_dir(workspace)
            (git_dir / "config").write_text(
                "[core]\n"
                "\trepositoryformatversion = 0\n"
                "[filter \"workspace-driver\"]\n"
                "\tclean = cat\n"
            )
            with self._open(database, workspace, artifacts) as operator:
                operator.grant(
                    actor_id="a-1",
                    capability_id="git.observe",
                    resource_scope=(".",),
                )
                with patch(
                    "ai_capital.product.capability_executors.subprocess.run"
                ) as run:
                    result = operator.invoke(
                        program_id="p-1",
                        actor_id="a-1",
                        capability_id="git.observe",
                        arguments={"path": ".", "operation": "status"},
                    )
            self.assertEqual(result["operation"]["execution_outcome"], "failed")
            run.assert_not_called()

    def test_git_observe_rejects_metadata_routing_before_subprocess(self):
        cases = ("gitfile", "commondir", "alternates")
        for case in cases:
            with self.subTest(case=case):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    database, workspace, artifacts = self._fixture(root)
                    if case == "gitfile":
                        (workspace / ".git").write_text(
                            f"gitdir: {(root / 'outside.git').as_posix()}\n"
                        )
                    else:
                        git_dir = self._minimal_git_dir(workspace)
                        if case == "commondir":
                            (git_dir / "commondir").write_text("../outside.git\n")
                        else:
                            info = git_dir / "objects" / "info"
                            info.mkdir(parents=True)
                            (info / "alternates").write_text(
                                f"{(root / 'outside-objects').as_posix()}\n"
                            )
                    with self._open(database, workspace, artifacts) as operator:
                        operator.grant(
                            actor_id="a-1",
                            capability_id="git.observe",
                            resource_scope=(".",),
                        )
                        with patch(
                            "ai_capital.product.capability_executors.subprocess.run"
                        ) as run:
                            result = operator.invoke(
                                program_id="p-1",
                                actor_id="a-1",
                                capability_id="git.observe",
                                arguments={"path": ".", "operation": "log"},
                            )
                    self.assertEqual(
                        result["operation"]["execution_outcome"], "failed"
                    )
                    run.assert_not_called()

    def test_active_grants_view_excludes_expired_grants(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace, artifacts = self._fixture(root)
            with self._open(database, workspace, artifacts) as operator:
                with patch(
                    "ai_capital.product.capability_operator.utc_now",
                    return_value="2026-01-01T00:00:00+00:00",
                ):
                    operator.grant(
                        actor_id="a-1",
                        capability_id="workspace.read",
                        resource_scope=("expired.txt",),
                        expires_at="2026-01-01T01:00:00+00:00",
                    )
                    operator.grant(
                        actor_id="a-1",
                        capability_id="workspace.read",
                        resource_scope=("current.txt",),
                    )
                with patch(
                    "ai_capital.product.capability_operator.utc_now",
                    return_value="2026-01-01T02:00:00+00:00",
                ):
                    grants = operator.grants("a-1")
            self.assertEqual(len(grants), 1)
            self.assertEqual(grants[0]["resource_scope"], ["current.txt"])

    def test_command_observe_rejects_option_style_ls_operand(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace, artifacts = self._fixture(root)
            (workspace / "-a").write_text("not an option\n")
            with self._open(database, workspace, artifacts) as operator:
                operator.grant(
                    actor_id="a-1",
                    capability_id="command.observe",
                    resource_scope=("ls -a",),
                )
                result = operator.invoke(
                    program_id="p-1",
                    actor_id="a-1",
                    capability_id="command.observe",
                    arguments={"command": "ls -a"},
                )
            self.assertEqual(result["operation"]["execution_outcome"], "failed")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO creation is unavailable")
    def test_command_observe_rejects_special_file_before_subprocess(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace, artifacts = self._fixture(root)
            os.mkfifo(workspace / "pipe")
            with self._open(database, workspace, artifacts) as operator:
                operator.grant(
                    actor_id="a-1",
                    capability_id="command.observe",
                    resource_scope=("cat pipe",),
                )
                with patch(
                    "ai_capital.product.capability_executors.subprocess.run"
                ) as run:
                    result = operator.invoke(
                        program_id="p-1",
                        actor_id="a-1",
                        capability_id="command.observe",
                        arguments={"command": "cat pipe"},
                    )
            self.assertEqual(result["operation"]["execution_outcome"], "failed")
            run.assert_not_called()

    def test_capability_operator_rejects_overlapping_workspace_and_artifact_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = (
                (root / "same", root / "same"),
                (root / "workspace", root / "workspace" / "artifacts"),
                (root / "artifacts" / "workspace", root / "artifacts"),
            )
            for index, (workspace, artifacts) in enumerate(cases):
                with self.subTest(index=index):
                    with self.assertRaises(InvalidRequest):
                        LocalCapabilityOperator.open(
                            root / f"overlap-{index}.db",
                            workspace_root=workspace,
                            artifact_root=artifacts,
                        )


if __name__ == "__main__":
    unittest.main()
