from __future__ import annotations

import os
from pathlib import Path
import tempfile
import time
import unittest

from ai_capital.kernel.actor_store import ActorRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ProgramStatus
from ai_capital.kernel.models import Actor, Program
from ai_capital.product import LocalCapabilityOperator, LocalProgramOperator


@unittest.skipIf(os.name == "nt", "descriptor-rooted reliability requires POSIX")
class H2ReliabilityReviewTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, Path, Path]:
        database = root / "capital.db"
        workspace = root / "workspace"
        artifacts = root / "generated-artifacts"
        workspace.mkdir()
        artifacts.mkdir()
        with ProgramRepository(database) as programs:
            programs.create(Program("p-1", 0, "H2.7 review remediation"))
            programs.transition("p-1", ProgramStatus.ACTIVE, expected_revision=0)
            ActorRepository(programs).register(Actor("a-1", 0, "worker", "configured-binding"))
        return database, workspace, artifacts

    def _open(self, database: Path, workspace: Path, artifacts: Path):
        return LocalCapabilityOperator.open(database, workspace_root=workspace, artifact_root=artifacts)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO creation is unavailable")
    def test_workspace_read_rejects_fifo_without_blocking(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace, artifacts = self._fixture(root)
            os.mkfifo(workspace / "pipe")
            with self._open(database, workspace, artifacts) as operator:
                operator.grant(actor_id="a-1", capability_id="workspace.read", resource_scope=("pipe",))
                started = time.monotonic()
                result = operator.invoke(program_id="p-1", actor_id="a-1", capability_id="workspace.read", arguments={"path": "pipe"})
                elapsed = time.monotonic() - started
            self.assertLess(elapsed, 2)
            self.assertEqual(result["operation"]["execution_outcome"], "failed")
            self.assertEqual(result["operation"]["effect_status"], "not_applicable")

    def test_workspace_root_swap_to_symlink_fails_closed(self):
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace, artifacts = self._fixture(root)
            pinned = root / "workspace-pinned"
            outside = root / "outside"
            outside.mkdir()
            with self._open(database, workspace, artifacts) as operator:
                operator.grant(actor_id="a-1", capability_id="workspace.write", resource_scope=("escape.txt",))
                workspace.rename(pinned)
                workspace.symlink_to(outside, target_is_directory=True)
                result = operator.invoke(program_id="p-1", actor_id="a-1", capability_id="workspace.write", arguments={"path": "escape.txt", "content": "must stay confined"})
            self.assertEqual(result["operation"]["execution_outcome"], "failed")
            self.assertFalse((outside / "escape.txt").exists())
            self.assertFalse((pinned / "escape.txt").exists())

    def test_approved_request_recovers_running_operation_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace, artifacts = self._fixture(root)
            arguments = {"path": "approved.txt", "content": "approved once\n"}
            with self._open(database, workspace, artifacts) as operator:
                operator.grant(actor_id="a-1", capability_id="workspace.write", resource_scope=("approved.txt",), approval_required=True)
                pending = operator.invoke(program_id="p-1", actor_id="a-1", capability_id="workspace.write", arguments=arguments, request_id="req-approved-restart")
            decision_id = pending["decision"]["decision_id"]
            with LocalProgramOperator.open(database) as programs:
                approved = programs.approve(decision_id)
                approval_id = approved["approval"]["receipt"]["approval_id"]

            with self._open(database, workspace, artifacts) as operator:
                context = operator._authority_store.get_decision(decision_id)
                authority = operator._authority.issue_execution_authority(decision_id=decision_id, approval_id=approval_id)
                operation = operator._journal.create_intent(program_id=context.program_id, actor_id=context.actor_id, resolution=context.resolution, authority_receipt_ref=authority.receipt_id)
                operator._authority.consume_execution_authority(receipt_id=authority.receipt_id)
                operator._journal.mark_admitted(operation.operation_id)
                operator._journal.mark_running(operation.operation_id)
                operation_id = operation.operation_id

            with self._open(database, workspace, artifacts) as operator:
                replay = operator.invoke(program_id="p-1", actor_id="a-1", capability_id="workspace.write", arguments=arguments, request_id="req-approved-restart")
                count = int(operator._programs._db.execute("SELECT COUNT(*) FROM operation_projections").fetchone()[0])
            self.assertEqual(replay["state"], "executed")
            self.assertEqual(replay["operation"]["operation_id"], operation_id)
            self.assertEqual(replay["operation"]["effect_status"], "indeterminate")
            self.assertEqual(replay["operation"]["reconciliation_status"], "pending")
            self.assertEqual(count, 1)
            self.assertFalse((workspace / "approved.txt").exists())


if __name__ == "__main__":
    unittest.main()
