from __future__ import annotations

from pathlib import Path
import os
import tempfile
import unittest

from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ProgramStatus
from ai_capital.kernel.errors import IntegrityViolation, InvalidRequest
from ai_capital.kernel.models import Program
from ai_capital.product import LocalWorkspaceOperator


class H2WorkspaceSnapshotTests(unittest.TestCase):
    def _program(self, database: Path, program_id: str = "p-1") -> None:
        with ProgramRepository(database) as programs:
            programs.create(Program(program_id, 0, "preserve workspace exactly"))

    def test_snapshot_survives_restart_and_browses_exact_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "capital.db"
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "z.txt").write_bytes(b"last\n")
            (workspace / "nested").mkdir()
            (workspace / "nested" / "a.bin").write_bytes(b"\x00\x01exact")
            self._program(database)

            with LocalWorkspaceOperator.open(database) as operator:
                captured = operator.snapshot("p-1")
                snapshot_id = captured["snapshot"]["snapshot_id"]
                self.assertEqual(
                    [item["path"] for item in captured["artifacts"]],
                    ["nested/a.bin", "z.txt"],
                )
                self.assertEqual(
                    operator.artifact_bytes(snapshot_id, "nested/a.bin"),
                    b"\x00\x01exact",
                )

            with LocalWorkspaceOperator.open(database) as restarted:
                listed = restarted.snapshots("p-1")
                self.assertEqual(len(listed), 1)
                self.assertEqual(listed[0], captured)
                self.assertEqual(
                    restarted.artifact_bytes(snapshot_id, "z.txt"), b"last\n"
                )

    def test_same_revision_and_content_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "capital.db"
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "result.txt").write_text("same", encoding="utf-8")
            self._program(database)
            with LocalWorkspaceOperator.open(database) as operator:
                first = operator.snapshot("p-1")
                second = operator.snapshot("p-1")
                self.assertEqual(first, second)
                self.assertEqual(len(operator.snapshots("p-1")), 1)

    def test_program_revision_is_part_of_snapshot_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "capital.db"
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "result.txt").write_text("same", encoding="utf-8")
            self._program(database)
            with LocalWorkspaceOperator.open(database) as operator:
                revision_zero = operator.snapshot("p-1")
            with ProgramRepository(database) as programs:
                programs.transition("p-1", ProgramStatus.ACTIVE, expected_revision=0)
            with LocalWorkspaceOperator.open(database) as operator:
                revision_one = operator.snapshot("p-1")
                self.assertNotEqual(
                    revision_zero["snapshot"]["snapshot_id"],
                    revision_one["snapshot"]["snapshot_id"],
                )
                self.assertEqual(revision_one["snapshot"]["program_revision"], 1)

    @unittest.skipIf(os.name == "nt", "symlink creation is not reliably available")
    def test_snapshot_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "capital.db"
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "target.txt").write_text("target", encoding="utf-8")
            (workspace / "link.txt").symlink_to(workspace / "target.txt")
            self._program(database)
            with LocalWorkspaceOperator.open(database) as operator:
                with self.assertRaises(InvalidRequest):
                    operator.snapshot("p-1")

    def test_artifact_tampering_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "capital.db"
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "result.txt").write_bytes(b"authentic")
            self._program(database)
            with LocalWorkspaceOperator.open(database) as operator:
                captured = operator.snapshot("p-1")
                entry = captured["artifacts"][0]
                digest = entry["digest"]
                snapshot_id = captured["snapshot"]["snapshot_id"]
            artifact_path = root / "artifacts" / digest[:2] / digest[2:]
            artifact_path.write_bytes(b"tampered")
            with LocalWorkspaceOperator.open(database) as restarted:
                with self.assertRaises(IntegrityViolation):
                    restarted.show_snapshot(snapshot_id)

    def test_missing_snapshot_entry_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "capital.db"
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "a.txt").write_text("a", encoding="utf-8")
            self._program(database)
            with LocalWorkspaceOperator.open(database) as operator:
                captured = operator.snapshot("p-1")
                snapshot_id = captured["snapshot"]["snapshot_id"]
            with ProgramRepository(database) as programs:
                programs._db.execute(
                    "DROP TRIGGER workspace_snapshot_entries_immutable_delete"
                )
                programs._db.execute(
                    "DELETE FROM workspace_snapshot_entries WHERE snapshot_id = ?",
                    (snapshot_id,),
                )
            with LocalWorkspaceOperator.open(database) as restarted:
                with self.assertRaises(IntegrityViolation):
                    restarted.show_snapshot(snapshot_id)

    def test_schema_version_non_integer_storage_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "capital.db"
            workspace = root / "workspace"
            workspace.mkdir()
            self._program(database)
            with LocalWorkspaceOperator.open(database):
                pass
            with ProgramRepository(database) as programs:
                programs._db.execute(
                    """
                    UPDATE component_schema SET version = X'31'
                    WHERE component = 'product_workspace_archive'
                    """
                )
            with self.assertRaises(IntegrityViolation):
                LocalWorkspaceOperator.open(database)

    def test_workspace_and_artifact_roots_must_not_overlap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "capital.db"
            workspace = root / "workspace"
            workspace.mkdir()
            self._program(database)
            with self.assertRaises(InvalidRequest):
                LocalWorkspaceOperator.open(
                    database,
                    workspace_root=workspace,
                    artifact_root=workspace / "artifacts",
                )


if __name__ == "__main__":
    unittest.main()
