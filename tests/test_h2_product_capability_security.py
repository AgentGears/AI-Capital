from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from ai_capital.kernel.actor_store import ActorRepository
from ai_capital.kernel.authority_store import AuthorityRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import EffectClass, ProgramStatus
from ai_capital.kernel.errors import InvalidRequest
from ai_capital.kernel.events import utc_now
from ai_capital.kernel.models import Actor, Grant, Program
from ai_capital.product import LocalCapabilityOperator
from ai_capital.product.git_repository_guard import validate_git_repository


class H2ProductCapabilitySecurityTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, Path, Path]:
        database = root / "capital.db"
        workspace = root / "workspace"
        artifacts = root / "generated-artifacts"
        workspace.mkdir()
        artifacts.mkdir()
        with ProgramRepository(database) as programs:
            programs.create(Program("p-1", 0, "H2.6 security review"))
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

    @staticmethod
    def _binding_count(database: Path) -> int:
        with ProgramRepository(database) as programs:
            row = programs._db.execute(
                "SELECT COUNT(*) FROM product_capability_root_bindings"
            ).fetchone()
            return int(row[0])

    def test_authority_store_is_rejected_inside_capability_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = (
                (
                    root / "workspace-case" / "capital.db",
                    root / "workspace-case",
                    root / "workspace-case-artifacts",
                ),
                (
                    root / "artifact-case" / "capital.db",
                    root / "artifact-case-workspace",
                    root / "artifact-case",
                ),
            )
            for index, (database, workspace, artifacts) in enumerate(cases):
                with self.subTest(index=index):
                    with self.assertRaisesRegex(
                        InvalidRequest,
                        "authority store paths must be outside capability roots",
                    ):
                        LocalCapabilityOperator.open(
                            database,
                            workspace_root=workspace,
                            artifact_root=artifacts,
                        )

    def test_durable_authority_is_bound_to_initial_capability_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace_a, artifacts_a = self._fixture(root)
            workspace_b = root / "workspace-b"
            artifacts_b = root / "generated-artifacts-b"

            with self._open(database, workspace_a, artifacts_a) as operator:
                issued = operator.grant(
                    actor_id="a-1",
                    capability_id="workspace.write",
                    resource_scope=("x.txt",),
                )
            self.assertEqual(issued["capability_scope"], ["workspace.write"])

            with self.assertRaisesRegex(
                InvalidRequest,
                "capability roots do not match the durable authority root binding",
            ):
                self._open(database, workspace_b, artifacts_b)
            self.assertFalse(workspace_b.exists())
            self.assertFalse(artifacts_b.exists())

            with self._open(database, workspace_a, artifacts_a) as operator:
                grants = operator.grants("a-1")
            self.assertEqual(len(grants), 1)
            self.assertEqual(grants[0]["grant_id"], issued["grant_id"])

    def test_unbound_store_with_existing_grant_refuses_root_adoption(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace, artifacts = self._fixture(root)
            with ProgramRepository(database) as programs:
                AuthorityRepository(programs).issue_grant(
                    Grant(
                        grant_id="legacy-grant",
                        subject_ref="actor:a-1",
                        capability_scope=("workspace.write",),
                        resource_scope=("x.txt",),
                        effect_ceiling=EffectClass.MODIFY,
                        constraints=(),
                        issued_at=utc_now(),
                        expires_at=None,
                        revision=0,
                    )
                )

            with self.assertRaisesRegex(
                InvalidRequest,
                "unbound authority store already contains durable rooted authority state",
            ):
                self._open(database, workspace, artifacts)
            self.assertEqual(self._binding_count(database), 0)

    def test_root_validation_failure_does_not_persist_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, _, artifacts = self._fixture(root)
            bad_workspace = root / "workspace-file"
            bad_workspace.write_text("not a directory\n")

            with self.assertRaisesRegex(InvalidRequest, "capability root must be a directory"):
                self._open(database, bad_workspace, artifacts)
            self.assertEqual(self._binding_count(database), 0)

            corrected_workspace = root / "workspace-corrected"
            with self._open(database, corrected_workspace, artifacts) as operator:
                self.assertTrue(operator.capabilities())
            self.assertEqual(self._binding_count(database), 1)

    def test_bom_prefixed_git_filter_config_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repo"
            git_dir = repository / ".git"
            git_dir.mkdir(parents=True)
            (git_dir / "config").write_bytes(
                b"\xef\xbb\xbf[filter \"driver\"]\n\trequired = true\n"
            )
            with self.assertRaises(InvalidRequest):
                validate_git_repository(repository)

    def test_command_observe_uses_trusted_executable_without_inherited_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace, artifacts = self._fixture(root)
            (workspace / "note.txt").write_text("observed\n")
            fake = workspace / "cat"
            fake.write_text("not selected\n")
            fake.chmod(0o755)
            completed = SimpleNamespace(returncode=0, stdout="observed\n", stderr="")
            with patch.dict(os.environ, {"PATH": f".{os.pathsep}{workspace}"}, clear=False):
                with self._open(database, workspace, artifacts) as operator:
                    operator.grant(
                        actor_id="a-1",
                        capability_id="command.observe",
                        resource_scope=("cat note.txt",),
                    )
                    with patch(
                        "ai_capital.product.capability_executors.subprocess.run",
                        return_value=completed,
                    ) as run:
                        result = operator.invoke(
                            program_id="p-1",
                            actor_id="a-1",
                            capability_id="command.observe",
                            arguments={"command": "cat note.txt"},
                        )
            self.assertEqual(result["operation"]["execution_outcome"], "succeeded")
            executable = Path(run.call_args.kwargs["executable"])
            self.assertTrue(executable.is_absolute())
            self.assertNotEqual(executable, fake.resolve())
            self.assertNotIn(workspace.resolve(), executable.parents)
            self.assertNotIn("PATH", run.call_args.kwargs["env"])
            self.assertFalse(run.call_args.kwargs["shell"])

    def test_git_observe_uses_trusted_executable_without_inherited_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace, artifacts = self._fixture(root)
            self._minimal_git_dir(workspace)
            fake = workspace / "git"
            fake.write_text("not selected\n")
            fake.chmod(0o755)
            completed = SimpleNamespace(returncode=0, stdout="", stderr="")
            with patch.dict(os.environ, {"PATH": f".{os.pathsep}{workspace}"}, clear=False):
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
                            arguments={"path": ".", "operation": "status"},
                        )
            self.assertEqual(result["operation"]["execution_outcome"], "succeeded")
            executable = Path(run.call_args.kwargs["executable"])
            self.assertTrue(executable.is_absolute())
            self.assertNotEqual(executable, fake.resolve())
            self.assertNotIn(workspace.resolve(), executable.parents)
            self.assertNotIn("PATH", run.call_args.kwargs["env"])
            self.assertFalse(run.call_args.kwargs["shell"])

    def test_command_observe_rejects_multiple_operands_under_wildcard_grant(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace, artifacts = self._fixture(root)
            allowed = workspace / "allowed"
            allowed.mkdir()
            (allowed / "file.txt").write_text("allowed\n")
            (workspace / "secret.txt").write_text("outside scope\n")
            with self._open(database, workspace, artifacts) as operator:
                operator.grant(
                    actor_id="a-1",
                    capability_id="command.observe",
                    resource_scope=("cat allowed/*",),
                )
                with patch(
                    "ai_capital.product.capability_executors.subprocess.run"
                ) as run:
                    result = operator.invoke(
                        program_id="p-1",
                        actor_id="a-1",
                        capability_id="command.observe",
                        arguments={"command": "cat allowed/file.txt secret.txt"},
                    )
            self.assertEqual(result["operation"]["execution_outcome"], "failed")
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
