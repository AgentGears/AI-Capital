from __future__ import annotations

import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from ai_capital.kernel.actor_store import ActorRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ProgramStatus
from ai_capital.kernel.errors import AuthorityDenied, ExecutionFailure, ExecutionTimeout, InvalidRequest
from ai_capital.kernel.models import Actor, Program
from ai_capital.product import LocalCapabilityOperator
from ai_capital.product.process_observation import run_bounded_process
from ai_capital.product import rooted_io


@unittest.skipIf(os.name == "nt", "rooted descriptor confinement requires POSIX dir_fd support")
class H2ReliabilityHardeningTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[Path, Path, Path]:
        database = root / "capital.db"
        workspace = root / "workspace"
        artifacts = root / "generated-artifacts"
        workspace.mkdir()
        artifacts.mkdir()
        with ProgramRepository(database) as programs:
            programs.create(Program("p-1", 0, "H2.7 reliability"))
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

    def test_exact_duplicate_request_replays_without_new_authority_or_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace, artifacts = self._fixture(root)
            with self._open(database, workspace, artifacts) as operator:
                operator.grant(
                    actor_id="a-1",
                    capability_id="workspace.write",
                    resource_scope=("duplicate.txt",),
                )
                first = operator.invoke(
                    program_id="p-1",
                    actor_id="a-1",
                    capability_id="workspace.write",
                    arguments={"path": "duplicate.txt", "content": "once\n"},
                    request_id="req-duplicate",
                )
                decision_count = int(
                    operator._programs._db.execute(
                        "SELECT COUNT(*) FROM authority_decisions"
                    ).fetchone()[0]
                )
                operation_count = int(
                    operator._programs._db.execute(
                        "SELECT COUNT(*) FROM operation_projections"
                    ).fetchone()[0]
                )
                second = operator.invoke(
                    program_id="p-1",
                    actor_id="a-1",
                    capability_id="workspace.write",
                    arguments={"path": "duplicate.txt", "content": "once\n"},
                    request_id="req-duplicate",
                )
                self.assertEqual(first, second)
                self.assertEqual(
                    int(
                        operator._programs._db.execute(
                            "SELECT COUNT(*) FROM authority_decisions"
                        ).fetchone()[0]
                    ),
                    decision_count,
                )
                self.assertEqual(
                    int(
                        operator._programs._db.execute(
                            "SELECT COUNT(*) FROM operation_projections"
                        ).fetchone()[0]
                    ),
                    operation_count,
                )
            self.assertEqual((workspace / "duplicate.txt").read_text(), "once\n")

    def test_duplicate_request_replays_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace, artifacts = self._fixture(root)
            with self._open(database, workspace, artifacts) as operator:
                operator.grant(
                    actor_id="a-1",
                    capability_id="artifact.write",
                    resource_scope=("result.txt",),
                )
                first = operator.invoke(
                    program_id="p-1",
                    actor_id="a-1",
                    capability_id="artifact.write",
                    arguments={"path": "result.txt", "content": "created once\n"},
                    request_id="req-restart",
                )
            with self._open(database, workspace, artifacts) as operator:
                second = operator.invoke(
                    program_id="p-1",
                    actor_id="a-1",
                    capability_id="artifact.write",
                    arguments={"path": "result.txt", "content": "created once\n"},
                    request_id="req-restart",
                )
                count = int(
                    operator._programs._db.execute(
                        "SELECT COUNT(*) FROM operation_projections"
                    ).fetchone()[0]
                )
            self.assertEqual(first, second)
            self.assertEqual(count, 1)
            self.assertEqual((artifacts / "result.txt").read_text(), "created once\n")

    def test_pre_h2_7_request_identity_is_migrated_without_repeating_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace, artifacts = self._fixture(root)
            with self._open(database, workspace, artifacts) as operator:
                operator.grant(
                    actor_id="a-1",
                    capability_id="workspace.write",
                    resource_scope=("legacy.txt",),
                )
                first = operator.invoke(
                    program_id="p-1",
                    actor_id="a-1",
                    capability_id="workspace.write",
                    arguments={"path": "legacy.txt", "content": "legacy once\n"},
                    request_id="req-legacy",
                )
                with operator._programs._transaction():
                    operator._programs._db.execute(
                        "DELETE FROM product_capability_requests WHERE request_id = ?",
                        ("req-legacy",),
                    )
                decisions = int(
                    operator._programs._db.execute(
                        "SELECT COUNT(*) FROM authority_decisions"
                    ).fetchone()[0]
                )
                operations = int(
                    operator._programs._db.execute(
                        "SELECT COUNT(*) FROM operation_projections"
                    ).fetchone()[0]
                )
            with self._open(database, workspace, artifacts) as operator:
                replay = operator.invoke(
                    program_id="p-1",
                    actor_id="a-1",
                    capability_id="workspace.write",
                    arguments={"path": "legacy.txt", "content": "legacy once\n"},
                    request_id="req-legacy",
                )
                self.assertEqual(
                    int(
                        operator._programs._db.execute(
                            "SELECT COUNT(*) FROM authority_decisions"
                        ).fetchone()[0]
                    ),
                    decisions,
                )
                self.assertEqual(
                    int(
                        operator._programs._db.execute(
                            "SELECT COUNT(*) FROM operation_projections"
                        ).fetchone()[0]
                    ),
                    operations,
                )
            self.assertEqual(first, replay)
            self.assertEqual((workspace / "legacy.txt").read_text(), "legacy once\n")

    def test_rejected_preconditions_do_not_leave_product_request_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace, artifacts = self._fixture(root)
            with self._open(database, workspace, artifacts) as operator:
                current = operator._programs.get("p-1")
                operator._programs.transition(
                    "p-1",
                    ProgramStatus.CANCELLED,
                    expected_revision=current.revision,
                )
                with self.assertRaises(AuthorityDenied):
                    operator.invoke(
                        program_id="p-1",
                        actor_id="a-1",
                        capability_id="workspace.write",
                        arguments={"path": "blocked.txt", "content": "blocked"},
                        request_id="req-precondition",
                    )
                count = int(
                    operator._programs._db.execute(
                        "SELECT COUNT(*) FROM product_capability_requests"
                    ).fetchone()[0]
                )
            self.assertEqual(count, 0)
            self.assertFalse((workspace / "blocked.txt").exists())

    def test_request_identity_reuse_with_different_payload_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace, artifacts = self._fixture(root)
            with self._open(database, workspace, artifacts) as operator:
                operator.grant(
                    actor_id="a-1",
                    capability_id="workspace.write",
                    resource_scope=("*",),
                )
                operator.invoke(
                    program_id="p-1",
                    actor_id="a-1",
                    capability_id="workspace.write",
                    arguments={"path": "one.txt", "content": "one"},
                    request_id="req-bound",
                )
                with self.assertRaisesRegex(
                    InvalidRequest,
                    "different invocation payload",
                ):
                    operator.invoke(
                        program_id="p-1",
                        actor_id="a-1",
                        capability_id="workspace.write",
                        arguments={"path": "two.txt", "content": "two"},
                        request_id="req-bound",
                    )
            self.assertFalse((workspace / "two.txt").exists())

    def test_pending_request_without_authority_recovers_as_interrupted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace, artifacts = self._fixture(root)
            payload = {
                "program_id": "p-1",
                "actor_id": "a-1",
                "capability_id": "workspace.write",
                "arguments": {"path": "never.txt", "content": "never"},
            }
            with self._open(database, workspace, artifacts) as operator:
                operator._requests.begin("req-interrupted", payload)
            with self._open(database, workspace, artifacts) as operator:
                result = operator.invoke(
                    program_id="p-1",
                    actor_id="a-1",
                    capability_id="workspace.write",
                    arguments={"path": "never.txt", "content": "never"},
                    request_id="req-interrupted",
                )
                self.assertEqual(result["state"], "interrupted")
                self.assertEqual(
                    result["reason_code"],
                    "interrupted_before_authority_decision",
                )
                self.assertEqual(
                    int(
                        operator._programs._db.execute(
                            "SELECT COUNT(*) FROM authority_decisions"
                        ).fetchone()[0]
                    ),
                    0,
                )
            self.assertFalse((workspace / "never.txt").exists())

    def test_stop_condition_at_dispatch_boundary_cancels_without_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace, artifacts = self._fixture(root)
            with self._open(database, workspace, artifacts) as operator:
                operator.grant(
                    actor_id="a-1",
                    capability_id="workspace.write",
                    resource_scope=("cancelled.txt",),
                )
                original = operator._require_program_ready
                calls = 0

                def stop_at_dispatch(program_id: str) -> None:
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        original(program_id)
                        return
                    current = operator._programs.get(program_id)
                    operator._programs.transition(
                        program_id,
                        ProgramStatus.CANCELLED,
                        expected_revision=current.revision,
                    )
                    raise AuthorityDenied("cancelled at dispatch boundary")

                with patch.object(
                    operator,
                    "_require_program_ready",
                    side_effect=stop_at_dispatch,
                ):
                    result = operator.invoke(
                        program_id="p-1",
                        actor_id="a-1",
                        capability_id="workspace.write",
                        arguments={"path": "cancelled.txt", "content": "must not exist"},
                    )
            self.assertEqual(result["operation"]["execution_outcome"], "cancelled")
            self.assertEqual(result["operation"]["effect_status"], "absent")
            self.assertIsNone(result["operation"]["started_at"])
            self.assertFalse((workspace / "cancelled.txt").exists())

    def test_workspace_read_enforces_product_byte_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace, artifacts = self._fixture(root)
            (workspace / "large.txt").write_bytes(b"x" * (1024 * 1024 + 1))
            with self._open(database, workspace, artifacts) as operator:
                operator.grant(
                    actor_id="a-1",
                    capability_id="workspace.read",
                    resource_scope=("large.txt",),
                )
                result = operator.invoke(
                    program_id="p-1",
                    actor_id="a-1",
                    capability_id="workspace.read",
                    arguments={"path": "large.txt"},
                )
            self.assertEqual(result["operation"]["execution_outcome"], "failed")
            self.assertEqual(result["operation"]["effect_status"], "not_applicable")

    def test_bounded_process_terminates_on_output_limit_and_timeout(self):
        environment = dict(os.environ)
        with self.assertRaisesRegex(ExecutionFailure, "output exceeds"):
            run_bounded_process(
                [sys.executable, "-c", "import sys; sys.stdout.write('x' * 65536)"],
                executable=sys.executable,
                cwd=Path.cwd(),
                env=environment,
                timeout_seconds=5,
                max_output_bytes=1024,
            )
        started = time.monotonic()
        with self.assertRaisesRegex(ExecutionTimeout, "timed out"):
            run_bounded_process(
                [sys.executable, "-c", "import time; time.sleep(5)"],
                executable=sys.executable,
                cwd=Path.cwd(),
                env=environment,
                timeout_seconds=0.05,
                max_output_bytes=1024,
            )
        self.assertLess(time.monotonic() - started, 3)

    def test_rooted_workspace_write_resists_parent_swap_to_symlink(self):
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace, artifacts = self._fixture(root)
            original_parent = workspace / "dir"
            original_parent.mkdir()
            outside = root / "outside"
            outside.mkdir()
            pinned = workspace / "pinned-dir"
            real_open_parent = rooted_io._open_parent
            swapped = False

            def swap_after_pin(base: Path, target: str, **kwargs):
                nonlocal swapped
                result = real_open_parent(base, target, **kwargs)
                if not swapped:
                    original_parent.rename(pinned)
                    original_parent.symlink_to(outside, target_is_directory=True)
                    swapped = True
                return result

            with self._open(database, workspace, artifacts) as operator:
                operator.grant(
                    actor_id="a-1",
                    capability_id="workspace.write",
                    resource_scope=("dir/value.txt",),
                )
                with patch(
                    "ai_capital.product.rooted_io._open_parent",
                    side_effect=swap_after_pin,
                ):
                    result = operator.invoke(
                        program_id="p-1",
                        actor_id="a-1",
                        capability_id="workspace.write",
                        arguments={"path": "dir/value.txt", "content": "pinned\n"},
                    )
            self.assertEqual(result["operation"]["execution_outcome"], "failed")
            self.assertFalse((outside / "value.txt").exists())
            self.assertFalse((pinned / "value.txt").exists())

    def test_materialized_files_have_explicit_private_mode_and_no_temp_residue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace, artifacts = self._fixture(root)
            with self._open(database, workspace, artifacts) as operator:
                operator.grant(
                    actor_id="a-1",
                    capability_id="workspace.write",
                    resource_scope=("mode.txt",),
                )
                result = operator.invoke(
                    program_id="p-1",
                    actor_id="a-1",
                    capability_id="workspace.write",
                    arguments={"path": "mode.txt", "content": "private\n"},
                )
            self.assertEqual(result["operation"]["execution_outcome"], "succeeded")
            self.assertEqual(stat.S_IMODE((workspace / "mode.txt").stat().st_mode), 0o600)
            self.assertFalse(
                any(path.name.endswith(".tmp") for path in workspace.iterdir())
            )


if __name__ == "__main__":
    unittest.main()
