from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


def _run(database: Path, *args: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    current = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = str(ROOT / "src") + (
        os.pathsep + current if current else ""
    )
    return subprocess.run(
        [sys.executable, "-m", "ai_capital.cli", "--database", str(database), *args],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


class H2WorkspaceCliTests(unittest.TestCase):
    def test_snapshot_artifact_and_bundle_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.db"
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "answer.txt").write_bytes(b"answer\n")

            created = _run(
                source,
                "create",
                "--program-id",
                "p-1",
                "--objective",
                "portable result",
            )
            self.assertEqual(created.returncode, 0, created.stderr)

            snap = _run(source, "snapshot", "p-1")
            self.assertEqual(snap.returncode, 0, snap.stderr)
            snap_json = json.loads(snap.stdout)
            snapshot_id = snap_json["snapshot"]["snapshot_id"]

            artifacts = _run(source, "artifacts", snapshot_id)
            self.assertEqual(artifacts.returncode, 0, artifacts.stderr)
            self.assertEqual(json.loads(artifacts.stdout)[0]["path"], "answer.txt")

            read = _run(source, "artifact-read", snapshot_id, "answer.txt")
            self.assertEqual(read.returncode, 0, read.stderr)
            self.assertEqual(
                base64.b64decode(json.loads(read.stdout)["content_base64"]),
                b"answer\n",
            )

            bundle_path = root / "program.bundle.json"
            exported = _run(
                source,
                "bundle-export",
                "p-1",
                snapshot_id,
                str(bundle_path),
            )
            self.assertEqual(exported.returncode, 0, exported.stderr)
            self.assertTrue(bundle_path.exists())

            target = root / "target.db"
            imported = _run(target, "bundle-import", str(bundle_path))
            self.assertEqual(imported.returncode, 0, imported.stderr)
            imported_json = json.loads(imported.stdout)
            bundle_id = imported_json["bundle"]["bundle_id"]

            listed = _run(target, "bundles")
            self.assertEqual(listed.returncode, 0, listed.stderr)
            self.assertEqual(json.loads(listed.stdout)[0]["bundle"]["bundle_id"], bundle_id)

            bundle_read = _run(target, "bundle-artifact-read", bundle_id, "answer.txt")
            self.assertEqual(bundle_read.returncode, 0, bundle_read.stderr)
            self.assertEqual(
                base64.b64decode(json.loads(bundle_read.stdout)["content_base64"]),
                b"answer\n",
            )

            live = _run(target, "list")
            self.assertEqual(live.returncode, 0, live.stderr)
            self.assertEqual(json.loads(live.stdout), [])

    def test_bundle_import_reports_invalid_archive_as_product_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "capital.db"
            bad = root / "bad.bundle.json"
            bad.write_text("not-json", encoding="utf-8")
            result = _run(database, "bundle-import", str(bad))
            self.assertEqual(result.returncode, 2)
            error = json.loads(result.stderr)["error"]
            self.assertEqual(error["code"], "InvalidRequest")


if __name__ == "__main__":
    unittest.main()
