from __future__ import annotations

import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest

from ai_capital.kernel.actor_store import ActorRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ProgramStatus
from ai_capital.kernel.errors import AICapitalError
from ai_capital.kernel.models import Actor, Program
from ai_capital.product import LocalCapabilityOperator, LocalProgramOperator


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        content = b"governed fetch\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, format, *args):
        return


class H2ProductCapabilityTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, Path, Path]:
        database = root / "capital.db"
        workspace = root / "workspace"
        artifacts = root / "generated-artifacts"
        workspace.mkdir()
        artifacts.mkdir()
        with ProgramRepository(database) as programs:
            programs.create(Program("p-1", 0, "governed local capability execution"))
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

    def test_profile_covers_six_product_families_without_implying_permission(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace, artifacts = self._fixture(root)
            with self._open(database, workspace, artifacts) as operator:
                profile = operator.capabilities()
                self.assertEqual(
                    {item["family"] for item in profile},
                    {
                        "filesystem",
                        "shell",
                        "http",
                        "git",
                        "structured_data",
                        "artifact_generation",
                    },
                )
                result = operator.invoke(
                    program_id="p-1",
                    actor_id="a-1",
                    capability_id="workspace.write",
                    arguments={"path": "notes.txt", "content": "denied"},
                )
                self.assertEqual(result["state"], "denied")
                self.assertIsNone(result["operation"])
                self.assertFalse((workspace / "notes.txt").exists())
                row = operator._programs._db.execute(
                    "SELECT COUNT(*) FROM operation_projections"
                ).fetchone()
                self.assertEqual(int(row[0]), 0)

    def test_explicit_grant_executes_workspace_write_and_links_auditable_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace, artifacts = self._fixture(root)
            with self._open(database, workspace, artifacts) as operator:
                operator.grant(
                    actor_id="a-1",
                    capability_id="workspace.write",
                    resource_scope=("notes.txt",),
                )
                result = operator.invoke(
                    program_id="p-1",
                    actor_id="a-1",
                    capability_id="workspace.write",
                    arguments={"path": "notes.txt", "content": "governed\n"},
                )
                operation_id = result["operation"]["operation_id"]
                self.assertEqual(result["state"], "executed")
                self.assertEqual(result["operation"]["execution_outcome"], "succeeded")
                self.assertEqual((workspace / "notes.txt").read_text(), "governed\n")

            with ProgramRepository(database) as programs:
                program = programs.get("p-1")
                self.assertIn(operation_id, program.operation_refs)
            with LocalProgramOperator.open(database) as operator:
                audit = operator.audit_operation(operation_id)
                self.assertEqual(audit["operation"]["operation_id"], operation_id)
                self.assertEqual(audit["events"][-1]["event_type"], "operation.finished")

    def test_approval_required_has_no_effect_until_approved_and_is_single_use(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace, artifacts = self._fixture(root)
            with self._open(database, workspace, artifacts) as operator:
                operator.grant(
                    actor_id="a-1",
                    capability_id="workspace.write",
                    resource_scope=("approved.txt",),
                    approval_required=True,
                )
                pending = operator.invoke(
                    program_id="p-1",
                    actor_id="a-1",
                    capability_id="workspace.write",
                    arguments={"path": "approved.txt", "content": "approved\n"},
                )
                self.assertEqual(pending["state"], "approval_required")
                self.assertFalse((workspace / "approved.txt").exists())
                decision_id = pending["decision"]["decision_id"]

            with LocalProgramOperator.open(database) as programs:
                approved = programs.approve(decision_id)
                approval_id = approved["approval"]["receipt"]["approval_id"]

            with self._open(database, workspace, artifacts) as operator:
                executed = operator.execute_approved(
                    decision_id=decision_id,
                    approval_id=approval_id,
                )
                self.assertEqual(executed["operation"]["execution_outcome"], "succeeded")
                self.assertEqual((workspace / "approved.txt").read_text(), "approved\n")
                with self.assertRaises(AICapitalError):
                    operator.execute_approved(
                        decision_id=decision_id,
                        approval_id=approval_id,
                    )

    def test_traversal_fails_closed_without_writing_outside_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace, artifacts = self._fixture(root)
            with self._open(database, workspace, artifacts) as operator:
                operator.grant(
                    actor_id="a-1",
                    capability_id="workspace.write",
                    resource_scope=("*",),
                )
                result = operator.invoke(
                    program_id="p-1",
                    actor_id="a-1",
                    capability_id="workspace.write",
                    arguments={"path": "../escape.txt", "content": "escape"},
                )
                self.assertEqual(result["operation"]["execution_outcome"], "failed")
                self.assertFalse((root / "escape.txt").exists())

    def test_structured_data_and_artifact_generation_are_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace, artifacts = self._fixture(root)
            with self._open(database, workspace, artifacts) as operator:
                for capability_id, target in (
                    ("structured.json.write", "data.json"),
                    ("structured.json.read", "data.json"),
                    ("artifact.write", "report.txt"),
                ):
                    operator.grant(
                        actor_id="a-1",
                        capability_id=capability_id,
                        resource_scope=(target,),
                    )
                written = operator.invoke(
                    program_id="p-1",
                    actor_id="a-1",
                    capability_id="structured.json.write",
                    arguments={"path": "data.json", "json": '{"b":2,"a":1}'},
                )
                self.assertEqual(written["operation"]["execution_outcome"], "succeeded")
                self.assertEqual((workspace / "data.json").read_text(), '{"a":1,"b":2}')
                read = operator.invoke(
                    program_id="p-1",
                    actor_id="a-1",
                    capability_id="structured.json.read",
                    arguments={"path": "data.json"},
                )
                self.assertEqual(
                    read["execution_receipt"]["output"]["canonical_json"],
                    '{"a":1,"b":2}',
                )
                generated = operator.invoke(
                    program_id="p-1",
                    actor_id="a-1",
                    capability_id="artifact.write",
                    arguments={"path": "report.txt", "content": "result\n"},
                )
                self.assertEqual(generated["operation"]["execution_outcome"], "succeeded")
                self.assertEqual((artifacts / "report.txt").read_text(), "result\n")

    def test_read_only_shell_and_git_use_fixed_non_shell_execution(self):
        if shutil.which("git") is None:
            self.skipTest("git is not installed")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace, artifacts = self._fixture(root)
            (workspace / "note.txt").write_text("observed\n")
            subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
            with self._open(database, workspace, artifacts) as operator:
                operator.grant(
                    actor_id="a-1",
                    capability_id="command.observe",
                    resource_scope=("cat note.txt",),
                )
                shell = operator.invoke(
                    program_id="p-1",
                    actor_id="a-1",
                    capability_id="command.observe",
                    arguments={"command": "cat note.txt"},
                )
                self.assertEqual(shell["execution_receipt"]["output"]["stdout"], "observed\n")
                operator.grant(
                    actor_id="a-1",
                    capability_id="git.observe",
                    resource_scope=(".",),
                )
                git = operator.invoke(
                    program_id="p-1",
                    actor_id="a-1",
                    capability_id="git.observe",
                    arguments={"path": ".", "operation": "status"},
                )
                self.assertEqual(git["operation"]["execution_outcome"], "succeeded")
                self.assertIn("note.txt", git["execution_receipt"]["output"]["stdout"])

    def test_http_fetch_runs_only_after_exact_grant(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace, artifacts = self._fixture(root)
            server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/result"
                with self._open(database, workspace, artifacts) as operator:
                    operator.grant(
                        actor_id="a-1",
                        capability_id="network.fetch",
                        resource_scope=(url,),
                    )
                    result = operator.invoke(
                        program_id="p-1",
                        actor_id="a-1",
                        capability_id="network.fetch",
                        arguments={"url": url},
                    )
                    output = result["execution_receipt"]["output"]
                    self.assertEqual(output["status"], 200)
                    self.assertEqual(base64.b64decode(output["content_base64"]), b"governed fetch\n")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
