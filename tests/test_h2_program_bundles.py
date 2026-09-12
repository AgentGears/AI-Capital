from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ProgramStatus
from ai_capital.kernel.errors import IntegrityViolation, InvalidRequest
from ai_capital.kernel.models import Program
from ai_capital.kernel.serialization import canonical_digest, canonical_json
from ai_capital.product import LocalWorkspaceOperator


class H2ProgramBundleTests(unittest.TestCase):
    def _source(self, root: Path) -> tuple[Path, str]:
        database = root / "source.db"
        workspace = root / "workspace"
        workspace.mkdir()
        (workspace / "result.txt").write_bytes(b"portable result\n")
        (workspace / "data.json").write_text('{"b":2,"a":1}', encoding="utf-8")
        with ProgramRepository(database) as programs:
            programs.create(Program("p-1", 0, "export this Program"))
        with LocalWorkspaceOperator.open(database) as operator:
            snapshot = operator.snapshot("p-1")
        return database, snapshot["snapshot"]["snapshot_id"]

    def test_export_is_byte_for_byte_deterministic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, snapshot_id = self._source(root)
            with LocalWorkspaceOperator.open(database) as operator:
                first = operator.export_bundle("p-1", snapshot_id)
                second = operator.export_bundle("p-1", snapshot_id)
                self.assertEqual(first, second)
                envelope = json.loads(first)
                self.assertTrue(envelope["bundle_id"].startswith("program-bundle:"))
                self.assertEqual(envelope["payload"]["source_program_id"], "p-1")

    def test_export_rejects_snapshot_from_old_program_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, snapshot_id = self._source(root)
            with ProgramRepository(database) as programs:
                programs.transition("p-1", ProgramStatus.ACTIVE, expected_revision=0)
            with LocalWorkspaceOperator.open(database) as operator:
                with self.assertRaises(InvalidRequest):
                    operator.export_bundle("p-1", snapshot_id)

    def test_import_persists_archive_without_creating_live_program(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, snapshot_id = self._source(root)
            with LocalWorkspaceOperator.open(source) as operator:
                content = operator.export_bundle("p-1", snapshot_id)

            target = root / "target.db"
            target_workspace = root / "target-workspace"
            target_artifacts = root / "target-artifacts"
            target_workspace.mkdir()
            with LocalWorkspaceOperator.open(
                target,
                workspace_root=target_workspace,
                artifact_root=target_artifacts,
            ) as operator:
                imported = operator.import_bundle(content)
                bundle_id = imported["bundle"]["bundle_id"]
                second = operator.import_bundle(content)
                self.assertEqual(second["imported_at"], imported["imported_at"])
                self.assertEqual(
                    operator.bundle_artifact_bytes(bundle_id, "result.txt"),
                    b"portable result\n",
                )

            with ProgramRepository(target) as programs:
                self.assertEqual(programs.list_programs(), ())

            with LocalWorkspaceOperator.open(
                target,
                workspace_root=target_workspace,
                artifact_root=target_artifacts,
            ) as restarted:
                listed = restarted.bundles()
                self.assertEqual(len(listed), 1)
                self.assertEqual(listed[0]["bundle"]["bundle_id"], bundle_id)

    def test_tampered_bundle_artifact_is_rejected_even_with_recomputed_bundle_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, snapshot_id = self._source(root)
            with LocalWorkspaceOperator.open(source) as operator:
                content = operator.export_bundle("p-1", snapshot_id)
            envelope = json.loads(content)
            envelope["payload"]["workspace"]["artifacts"][0]["content_base64"] = "dGFtcGVyZWQ="
            envelope["bundle_id"] = "program-bundle:" + canonical_digest(envelope["payload"])
            tampered = canonical_json(envelope).encode("utf-8")

            target = root / "target.db"
            target_workspace = root / "target-workspace"
            target_artifacts = root / "target-artifacts"
            target_workspace.mkdir()
            with LocalWorkspaceOperator.open(
                target,
                workspace_root=target_workspace,
                artifact_root=target_artifacts,
            ) as operator:
                with self.assertRaises(InvalidRequest):
                    operator.import_bundle(tampered)

    def test_tampered_bundle_control_anchor_is_rejected_with_recomputed_bundle_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, snapshot_id = self._source(root)
            with LocalWorkspaceOperator.open(source) as operator:
                content = operator.export_bundle("p-1", snapshot_id)
            envelope = json.loads(content)
            control = envelope["payload"]["control"]
            control.update(
                {
                    "revision": 1,
                    "program_revision": 0,
                    "paused": True,
                    "last_reason_code": "user_paused",
                    "changed_at": "2026-09-12T00:00:00+00:00",
                }
            )
            envelope["bundle_id"] = "program-bundle:" + canonical_digest(envelope["payload"])
            tampered = canonical_json(envelope).encode("utf-8")

            target = root / "target.db"
            target_workspace = root / "target-workspace"
            target_artifacts = root / "target-artifacts"
            target_workspace.mkdir()
            with LocalWorkspaceOperator.open(
                target,
                workspace_root=target_workspace,
                artifact_root=target_artifacts,
            ) as operator:
                with self.assertRaises(InvalidRequest):
                    operator.import_bundle(tampered)

    def test_noncanonical_bundle_encoding_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, snapshot_id = self._source(root)
            with LocalWorkspaceOperator.open(source) as operator:
                content = operator.export_bundle("p-1", snapshot_id)
            noncanonical = content + b"\n"
            target = root / "target.db"
            target_workspace = root / "target-workspace"
            target_artifacts = root / "target-artifacts"
            target_workspace.mkdir()
            with LocalWorkspaceOperator.open(
                target,
                workspace_root=target_workspace,
                artifact_root=target_artifacts,
            ) as operator:
                with self.assertRaises(InvalidRequest):
                    operator.import_bundle(noncanonical)

    def test_imported_bundle_row_tampering_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, snapshot_id = self._source(root)
            with LocalWorkspaceOperator.open(source) as operator:
                content = operator.export_bundle("p-1", snapshot_id)
            target = root / "target.db"
            target_workspace = root / "target-workspace"
            target_artifacts = root / "target-artifacts"
            target_workspace.mkdir()
            with LocalWorkspaceOperator.open(
                target,
                workspace_root=target_workspace,
                artifact_root=target_artifacts,
            ) as operator:
                imported = operator.import_bundle(content)
                bundle_id = imported["bundle"]["bundle_id"]
            with ProgramRepository(target) as programs:
                programs._db.execute(
                    "DROP TRIGGER program_bundle_imports_immutable_update"
                )
                programs._db.execute(
                    "UPDATE program_bundle_imports SET bundle_digest = ? WHERE bundle_id = ?",
                    ("0" * 64, bundle_id),
                )
            with LocalWorkspaceOperator.open(
                target,
                workspace_root=target_workspace,
                artifact_root=target_artifacts,
            ) as restarted:
                with self.assertRaises(IntegrityViolation):
                    restarted.show_bundle(bundle_id)


if __name__ == "__main__":
    unittest.main()
