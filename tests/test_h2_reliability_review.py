from __future__ import annotations

import os
import sys
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from ai_capital.kernel.actor_store import ActorRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ProgramStatus
from ai_capital.kernel.errors import ExecutionTimeout
from ai_capital.kernel.models import Actor, Program
from ai_capital.product import LocalCapabilityOperator, LocalProgramOperator
from ai_capital.product import rooted_io
from ai_capital.product.process_observation import run_bounded_process


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

    def test_workspace_read_rejects_in_place_mutation_during_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace, artifacts = self._fixture(root)
            target = workspace / "changing.txt"
            target.write_text("before\n")
            real_read = rooted_io._read_bounded

            def read_then_mutate(descriptor: int, *, max_bytes: int) -> bytes:
                content = real_read(descriptor, max_bytes=max_bytes)
                target.write_text("after!\n")
                return content

            with self._open(database, workspace, artifacts) as operator:
                operator.grant(
                    actor_id="a-1",
                    capability_id="workspace.read",
                    resource_scope=("changing.txt",),
                )
                with patch(
                    "ai_capital.product.rooted_io._read_bounded",
                    side_effect=read_then_mutate,
                ):
                    result = operator.invoke(
                        program_id="p-1",
                        actor_id="a-1",
                        capability_id="workspace.read",
                        arguments={"path": "changing.txt"},
                    )
            self.assertEqual(result["operation"]["execution_outcome"], "failed")
            self.assertEqual(result["operation"]["effect_status"], "not_applicable")

    def test_timeout_kills_descendant_after_leader_exits(self):
        child = (
            "import signal,time;"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
            "time.sleep(10)"
        )
        leader = (
            "import subprocess,sys,time;"
            "subprocess.Popen([sys.executable,'-c',sys.argv[1]]);"
            "time.sleep(10)"
        )
        started = time.monotonic()
        with self.assertRaises(ExecutionTimeout):
            run_bounded_process(
                [sys.executable, "-c", leader, child],
                executable=sys.executable,
                cwd=Path.cwd(),
                env=dict(os.environ),
                timeout_seconds=0.1,
                max_output_bytes=1024,
            )
        self.assertLess(time.monotonic() - started, 3)

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

    def test_workspace_write_rejects_final_component_replacement_before_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace, artifacts = self._fixture(root)
            target = workspace / "race.txt"
            target.write_text("initial\n")
            original_write_all = rooted_io._write_all
            swapped = False

            def replace_target_after_temp_write(descriptor: int, content: bytes) -> None:
                nonlocal swapped
                original_write_all(descriptor, content)
                if not swapped:
                    replacement = workspace / "replacement.txt"
                    replacement.write_text("concurrent\n")
                    os.replace(replacement, target)
                    swapped = True

            with self._open(database, workspace, artifacts) as operator:
                operator.grant(
                    actor_id="a-1",
                    capability_id="workspace.write",
                    resource_scope=("race.txt",),
                )
                with patch(
                    "ai_capital.product.rooted_io._write_all",
                    side_effect=replace_target_after_temp_write,
                ):
                    result = operator.invoke(
                        program_id="p-1",
                        actor_id="a-1",
                        capability_id="workspace.write",
                        arguments={"path": "race.txt", "content": "authorized\n"},
                    )
            self.assertEqual(result["operation"]["execution_outcome"], "failed")
            self.assertEqual(target.read_text(), "concurrent\n")
            self.assertFalse(any(item.name.endswith(".tmp") for item in workspace.iterdir()))

    def test_workspace_write_preserves_latest_replacement_without_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace, artifacts = self._fixture(root)
            target = workspace / "commit-race.txt"
            target.write_text("initial\n")
            original_write_all = rooted_io._write_all
            injected = False

            def replace_path_twice(descriptor: int, content: bytes) -> None:
                nonlocal injected
                original_write_all(descriptor, content)
                if injected:
                    return
                first = workspace / "concurrent-one.txt"
                first.write_text("concurrent-one\n")
                os.replace(first, target)
                second = workspace / "concurrent-two.txt"
                second.write_text("concurrent-two\n")
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
                        arguments={"path": "commit-race.txt", "content": "authorized\n"},
                    )
            self.assertEqual(result["operation"]["execution_outcome"], "failed")
            self.assertEqual(result["operation"]["effect_status"], "indeterminate")
            self.assertEqual(target.read_text(), "concurrent-two\n")
            self.assertFalse(any(item.name.endswith(".tmp") for item in workspace.iterdir()))

    def test_approved_request_recovers_issued_authority_before_operation_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace, artifacts = self._fixture(root)
            arguments = {"path": "approved-issued.txt", "content": "approved once\n"}
            with self._open(database, workspace, artifacts) as operator:
                operator.grant(
                    actor_id="a-1",
                    capability_id="workspace.write",
                    resource_scope=("approved-issued.txt",),
                    approval_required=True,
                )
                pending = operator.invoke(
                    program_id="p-1",
                    actor_id="a-1",
                    capability_id="workspace.write",
                    arguments=arguments,
                    request_id="req-approved-issued-restart",
                )
            decision_id = pending["decision"]["decision_id"]
            with LocalProgramOperator.open(database) as programs:
                approved = programs.approve(decision_id)
                approval_id = approved["approval"]["receipt"]["approval_id"]

            with self._open(database, workspace, artifacts) as operator:
                issued = operator._authority.issue_execution_authority(
                    decision_id=decision_id,
                    approval_id=approval_id,
                )
                issued_id = issued.receipt_id
                self.assertEqual(
                    int(
                        operator._programs._db.execute(
                            "SELECT COUNT(*) FROM operation_projections"
                        ).fetchone()[0]
                    ),
                    0,
                )

            with self._open(database, workspace, artifacts) as operator:
                replay = operator.invoke(
                    program_id="p-1",
                    actor_id="a-1",
                    capability_id="workspace.write",
                    arguments=arguments,
                    request_id="req-approved-issued-restart",
                )
                authorities = operator._programs._db.execute(
                    "SELECT receipt_id, consumed_at FROM execution_authority_receipts"
                ).fetchall()
                operation_count = int(
                    operator._programs._db.execute(
                        "SELECT COUNT(*) FROM operation_projections"
                    ).fetchone()[0]
                )
            self.assertEqual(replay["state"], "executed")
            self.assertEqual(len(authorities), 1)
            self.assertEqual(str(authorities[0]["receipt_id"]), issued_id)
            self.assertIsNotNone(authorities[0]["consumed_at"])
            self.assertEqual(operation_count, 1)
            self.assertEqual((workspace / "approved-issued.txt").read_text(), "approved once\n")

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


class H2WindowsQualificationTests(unittest.TestCase):
    def test_root_identity_defers_unsupported_rooted_io_until_invocation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(rooted_io.os, "name", "nt"):
                self.assertIsNone(rooted_io.root_identity(root))


if __name__ == "__main__":
    unittest.main()
