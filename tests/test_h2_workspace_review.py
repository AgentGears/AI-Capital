from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.errors import IntegrityViolation, InvalidRequest
from ai_capital.kernel.models import Program
from ai_capital.kernel.serialization import canonical_digest, canonical_json
from ai_capital.product import LocalWorkspaceOperator
from ai_capital.product.workspace_blobs import WorkspaceBlobStore
from ai_capital.product.workspace_types import sha256_bytes


class H2WorkspaceReviewRegressionTests(unittest.TestCase):
    def _source(self, root: Path) -> tuple[Path, str, str]:
        database = root / "source.db"
        workspace = root / "workspace"
        workspace.mkdir()
        (workspace / "result.txt").write_bytes(b"review artifact")
        with ProgramRepository(database) as programs:
            programs.create(Program("p-1", 0, "review H2.5"))
        with LocalWorkspaceOperator.open(database) as operator:
            captured = operator.snapshot("p-1")
            snapshot_id = captured["snapshot"]["snapshot_id"]
            digest = captured["artifacts"][0]["digest"]
        return database, snapshot_id, digest

    def test_oversized_corrupt_blob_is_rejected_before_opening_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, snapshot_id, digest = self._source(root)
            artifact_path = root / "artifacts" / digest[:2] / digest[2:]
            artifact_path.write_bytes(b"review artifact" + b"x")
            with LocalWorkspaceOperator.open(database) as operator:
                with mock.patch.object(
                    Path,
                    "open",
                    side_effect=AssertionError("oversized artifact must not be opened"),
                ):
                    with self.assertRaises(IntegrityViolation):
                        operator.show_snapshot(snapshot_id)

    def test_new_artifact_directory_entries_are_flushed_before_metadata_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "capital.db"
            artifact_root = root / "archive" / "artifacts"
            with ProgramRepository(database) as programs:
                with mock.patch.object(
                    WorkspaceBlobStore,
                    "_fsync_directory",
                ) as flush:
                    store = WorkspaceBlobStore(programs, artifact_root)
                    content = b"durable artifact"
                    digest = sha256_bytes(content)
                    store.store_file(content, digest)
            flushed = [call.args[0] for call in flush.call_args_list]
            prefix = artifact_root.resolve() / digest[:2]
            self.assertIn(artifact_root.resolve().parent, flushed)
            self.assertIn(artifact_root.resolve(), flushed)
            self.assertIn(prefix, flushed)

    def test_recomputed_bundle_with_malformed_ask_audit_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, snapshot_id, _ = self._source(root)
            with LocalWorkspaceOperator.open(database) as operator:
                content = operator.export_bundle("p-1", snapshot_id)
            envelope = json.loads(content)
            envelope["payload"]["audit"]["asks"].append({"program_id": "p-1"})
            envelope["bundle_id"] = "program-bundle:" + canonical_digest(
                envelope["payload"]
            )
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


if __name__ == "__main__":
    unittest.main()
