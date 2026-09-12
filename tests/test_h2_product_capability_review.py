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

    def test_git_observe_disables_configured_helpers_and_text_conversion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace, artifacts = self._fixture(root)
            completed = SimpleNamespace(returncode=0, stdout="", stderr="")
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
            self.assertEqual(argv[:4], ["git", "-c", "core.fsmonitor=false", "diff"])
            self.assertIn("--no-ext-diff", argv)
            self.assertIn("--no-textconv", argv)
            self.assertFalse(run.call_args.kwargs["shell"])

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


if __name__ == "__main__":
    unittest.main()
