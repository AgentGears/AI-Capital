from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.errors import IntegrityViolation, InvalidStateTransition, StaleProgramRevision
from ai_capital.kernel.models import Program
from ai_capital.product import LocalProgramOperator


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


class H2LocalProgramOperatorTests(unittest.TestCase):
    def test_local_api_create_list_show_and_restart_preserve_exact_state(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "capital.db"
            with LocalProgramOperator.open(database) as operator:
                second = operator.create(
                    program_id="p-b",
                    objective="second objective",
                    constraints=("bounded",),
                )
                first = operator.create(
                    program_id="p-a",
                    objective="first objective",
                    success_criteria=("artifact exists",),
                )
                listed = operator.list()
                self.assertEqual(
                    [item["program"]["program_id"] for item in listed],
                    ["p-a", "p-b"],
                )
                self.assertEqual(operator.show("p-a"), first)
                self.assertEqual(operator.show("p-b"), second)
                self.assertEqual(first["event_count"], 1)
                self.assertEqual(second["event_count"], 1)

            with LocalProgramOperator.open(database) as restarted:
                self.assertEqual(restarted.show("p-a"), first)
                self.assertEqual(restarted.show("p-b"), second)
                self.assertEqual(
                    [item["program"]["program_id"] for item in restarted.list()],
                    ["p-a", "p-b"],
                )

    def test_start_cancel_and_revision_guards_delegate_to_kernel_lifecycle(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "capital.db"
            with LocalProgramOperator.open(database) as operator:
                created = operator.create(program_id="p-1", objective="bounded work")
                self.assertEqual(created["program"]["status"], "created")
                self.assertEqual(created["program"]["revision"], 0)

                active = operator.start("p-1", expected_revision=0)
                self.assertEqual(active["program"]["status"], "active")
                self.assertEqual(active["program"]["revision"], 1)
                self.assertEqual(active["event_count"], 2)

                with self.assertRaises(StaleProgramRevision):
                    operator.start("p-1", expected_revision=0)

                cancelled = operator.cancel("p-1", expected_revision=1)
                self.assertEqual(cancelled["program"]["status"], "cancelled")
                self.assertEqual(cancelled["program"]["revision"], 2)
                self.assertEqual(cancelled["event_count"], 3)

                with self.assertRaises(InvalidStateTransition):
                    operator.cancel("p-1", expected_revision=2)

    def test_program_listing_authenticates_each_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "capital.db"
            with ProgramRepository(database) as programs:
                programs.create(Program("p-1", 0, "integrity"))
                programs._db.execute(
                    "UPDATE program_projections SET projection_digest = ? WHERE program_id = ?",
                    ("0" * 64, "p-1"),
                )
                with self.assertRaises(IntegrityViolation):
                    programs.list_programs()

    def test_cli_uses_durable_operator_path_and_emits_deterministic_json(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "capital.db"

            created = _run_cli(
                database,
                "create",
                "--program-id",
                "p-1",
                "--objective",
                "ship bounded work",
                "--constraint",
                "local only",
                "--success-criterion",
                "result is inspectable",
            )
            self.assertEqual(created.returncode, 0, created.stderr)
            created_json = json.loads(created.stdout)
            self.assertEqual(created_json["program"]["status"], "created")
            self.assertEqual(created_json["event_count"], 1)

            listed = _run_cli(database, "list")
            self.assertEqual(listed.returncode, 0, listed.stderr)
            self.assertEqual(json.loads(listed.stdout), [created_json])

            started = _run_cli(
                database,
                "start",
                "p-1",
                "--expected-revision",
                "0",
            )
            self.assertEqual(started.returncode, 0, started.stderr)
            started_json = json.loads(started.stdout)
            self.assertEqual(started_json["program"]["status"], "active")
            self.assertEqual(started_json["program"]["revision"], 1)

            stale = _run_cli(
                database,
                "start",
                "p-1",
                "--expected-revision",
                "0",
            )
            self.assertEqual(stale.returncode, 2)
            self.assertEqual(stale.stdout, "")
            stale_error = json.loads(stale.stderr)
            self.assertEqual(stale_error["error"]["code"], "StaleProgramRevision")

            cancelled = _run_cli(
                database,
                "cancel",
                "p-1",
                "--expected-revision",
                "1",
            )
            self.assertEqual(cancelled.returncode, 0, cancelled.stderr)
            cancelled_json = json.loads(cancelled.stdout)
            self.assertEqual(cancelled_json["program"]["status"], "cancelled")
            self.assertEqual(cancelled_json["program"]["revision"], 2)
            self.assertEqual(cancelled_json["event_count"], 3)

            shown = _run_cli(database, "show", "p-1")
            self.assertEqual(shown.returncode, 0, shown.stderr)
            self.assertEqual(json.loads(shown.stdout), cancelled_json)


if __name__ == "__main__":
    unittest.main()
