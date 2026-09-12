from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from ai_capital.kernel.actor_store import ActorRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ProgramStatus
from ai_capital.kernel.models import Actor, Program


ROOT = Path(__file__).resolve().parents[1]


def _run_cli(database: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    current = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(ROOT / "src") + (os.pathsep + current if current else "")
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "ai_capital.cli",
            "--database",
            str(database),
            *args,
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


class H2CapabilityCliTests(unittest.TestCase):
    def test_cli_lists_grants_and_invokes_through_governed_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "capital.db"
            workspace = root / "workspace"
            workspace.mkdir()
            with ProgramRepository(database) as programs:
                programs.create(Program("p-1", 0, "CLI capability execution"))
                programs.transition("p-1", ProgramStatus.ACTIVE, expected_revision=0)
                ActorRepository(programs).register(
                    Actor("a-1", 0, "worker", "configured-binding")
                )

            profile = _run_cli(database, "capabilities")
            self.assertEqual(profile.returncode, 0, profile.stderr)
            families = {item["family"] for item in json.loads(profile.stdout)}
            self.assertIn("filesystem", families)
            self.assertIn("artifact_generation", families)

            grant = _run_cli(
                database,
                "capability-grant",
                "a-1",
                "workspace.write",
                "--resource-scope",
                "notes.txt",
            )
            self.assertEqual(grant.returncode, 0, grant.stderr)
            grant_json = json.loads(grant.stdout)
            self.assertEqual(grant_json["capability_scope"], ["workspace.write"])

            grants = _run_cli(database, "capability-grants", "a-1")
            self.assertEqual(grants.returncode, 0, grants.stderr)
            self.assertEqual(len(json.loads(grants.stdout)), 1)

            invoke = _run_cli(
                database,
                "capability-invoke",
                "p-1",
                "a-1",
                "workspace.write",
                "--arguments-json",
                '{"path":"notes.txt","content":"from CLI\\n"}',
            )
            self.assertEqual(invoke.returncode, 0, invoke.stderr)
            result = json.loads(invoke.stdout)
            self.assertEqual(result["state"], "executed")
            self.assertEqual(result["operation"]["execution_outcome"], "succeeded")
            self.assertEqual((workspace / "notes.txt").read_text(), "from CLI\n")

            revoke = _run_cli(
                database,
                "capability-revoke",
                grant_json["grant_id"],
            )
            self.assertEqual(revoke.returncode, 0, revoke.stderr)
            self.assertTrue(json.loads(revoke.stdout)["revoked"])

    def test_cli_invalid_arguments_json_fails_without_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "capital.db"
            (root / "workspace").mkdir()
            with ProgramRepository(database) as programs:
                programs.create(Program("p-1", 0, "invalid CLI arguments"))
                programs.transition("p-1", ProgramStatus.ACTIVE, expected_revision=0)
                ActorRepository(programs).register(
                    Actor("a-1", 0, "worker", "configured-binding")
                )
            result = _run_cli(
                database,
                "capability-invoke",
                "p-1",
                "a-1",
                "workspace.read",
                "--arguments-json",
                "[]",
            )
            self.assertEqual(result.returncode, 2)
            error = json.loads(result.stderr)["error"]
            self.assertEqual(error["code"], "InvalidRequest")


if __name__ == "__main__":
    unittest.main()
