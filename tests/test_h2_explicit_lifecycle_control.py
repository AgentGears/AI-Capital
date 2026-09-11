from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import (
    EffectClass,
    EffectStatus,
    ExecutionOutcome,
    ProgramStatus,
)
from ai_capital.kernel.errors import (
    IntegrityViolation,
    InvalidStateTransition,
    StaleProgramControlRevision,
    StaleProgramRevision,
)
from ai_capital.kernel.models import CapabilityResolution, Program, ResolvedEffect
from ai_capital.kernel.operation_journal import ExecutionObservation, OperationJournal
from ai_capital.kernel.program_control import ProgramControlRepository
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


class H2ExplicitLifecycleControlTests(unittest.TestCase):
    def test_pause_resume_survive_restart_without_mutating_program_history(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "capital.db"
            with LocalProgramOperator.open(database) as operator:
                operator.create(program_id="p-1", objective="durable pause")
                active = operator.start("p-1", expected_revision=0)
                self.assertEqual(active["program"]["revision"], 1)
                self.assertEqual(active["event_count"], 2)

                paused = operator.pause(
                    "p-1",
                    expected_revision=1,
                    expected_control_revision=0,
                )
                self.assertEqual(paused["program"]["status"], "active")
                self.assertEqual(paused["program"]["revision"], 1)
                self.assertEqual(paused["event_count"], 2)
                self.assertEqual(paused["control"]["revision"], 1)
                self.assertEqual(paused["control"]["program_revision"], 1)
                self.assertTrue(paused["control"]["paused"])
                self.assertEqual(paused["lifecycle"]["execution_state"], "paused")
                self.assertEqual(paused["lifecycle"]["reason_code"], "user_paused")

            with LocalProgramOperator.open(database) as restarted:
                self.assertEqual(restarted.show("p-1"), paused)
                resumed = restarted.resume(
                    "p-1",
                    expected_revision=1,
                    expected_control_revision=1,
                )
                self.assertEqual(resumed["program"]["status"], "active")
                self.assertEqual(resumed["program"]["revision"], 1)
                self.assertEqual(resumed["event_count"], 2)
                self.assertEqual(resumed["control"]["revision"], 2)
                self.assertEqual(resumed["control"]["program_revision"], 1)
                self.assertFalse(resumed["control"]["paused"])
                self.assertEqual(resumed["lifecycle"]["execution_state"], "running")
                history = restarted._controls.history("p-1")
                self.assertEqual([item.revision for item in history], [1, 2])
                self.assertEqual([item.paused for item in history], [True, False])
                self.assertEqual([item.program_revision for item in history], [1, 1])

            with LocalProgramOperator.open(database) as restarted_again:
                self.assertEqual(restarted_again.show("p-1"), resumed)

    def test_stale_program_and_control_revisions_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "capital.db"
            with LocalProgramOperator.open(database) as operator:
                operator.create(program_id="p-1", objective="revision guards")
                operator.start("p-1", expected_revision=0)

                with self.assertRaises(StaleProgramRevision):
                    operator.pause(
                        "p-1",
                        expected_revision=0,
                        expected_control_revision=0,
                    )
                self.assertEqual(operator.show("p-1")["control"]["revision"], 0)

                operator.pause(
                    "p-1",
                    expected_revision=1,
                    expected_control_revision=0,
                )
                with self.assertRaises(StaleProgramControlRevision):
                    operator.resume(
                        "p-1",
                        expected_revision=1,
                        expected_control_revision=0,
                    )
                shown = operator.show("p-1")
                self.assertEqual(shown["program"]["revision"], 1)
                self.assertEqual(shown["event_count"], 2)
                self.assertEqual(shown["control"]["revision"], 1)
                self.assertTrue(shown["control"]["paused"])

    def test_resume_never_clears_a_host_block(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "capital.db"
            with LocalProgramOperator.open(database) as operator:
                operator.create(program_id="p-1", objective="host block")
                operator.start("p-1", expected_revision=0)
                operator.pause(
                    "p-1",
                    expected_revision=1,
                    expected_control_revision=0,
                )
                blocked = operator._programs.transition(
                    "p-1",
                    ProgramStatus.BLOCKED,
                    expected_revision=1,
                )
                self.assertEqual(blocked.revision, 2)

                with self.assertRaises(InvalidStateTransition):
                    operator.resume(
                        "p-1",
                        expected_revision=2,
                        expected_control_revision=1,
                    )
                shown = operator.show("p-1")
                self.assertEqual(shown["program"]["status"], "blocked")
                self.assertEqual(shown["program"]["revision"], 2)
                self.assertTrue(shown["control"]["paused"])
                self.assertEqual(shown["lifecycle"]["execution_state"], "blocked")
                self.assertEqual(shown["lifecycle"]["reason_code"], "host_blocked")

    def test_cancel_paused_program_keeps_control_history_auditable(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "capital.db"
            with LocalProgramOperator.open(database) as operator:
                operator.create(program_id="p-1", objective="cancel paused")
                operator.start("p-1", expected_revision=0)
                operator.pause(
                    "p-1",
                    expected_revision=1,
                    expected_control_revision=0,
                )
                cancelled = operator.cancel("p-1", expected_revision=1)
                self.assertEqual(cancelled["program"]["status"], "cancelled")
                self.assertEqual(cancelled["program"]["revision"], 2)
                self.assertEqual(cancelled["control"]["revision"], 1)
                self.assertEqual(cancelled["control"]["program_revision"], 1)
                self.assertTrue(cancelled["control"]["paused"])
                self.assertEqual(cancelled["lifecycle"]["execution_state"], "cancelled")
                self.assertEqual(cancelled["lifecycle"]["reason_code"], "program_cancelled")
                history = operator._controls.history("p-1")
                self.assertEqual(len(history), 1)
                self.assertTrue(history[0].paused)

    def test_corrupted_control_projection_and_missing_current_schema_table_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "capital.db"
            with LocalProgramOperator.open(database) as operator:
                operator.create(program_id="p-1", objective="integrity")
                operator.start("p-1", expected_revision=0)
                operator.pause(
                    "p-1",
                    expected_revision=1,
                    expected_control_revision=0,
                )
                operator._programs._db.execute(
                    """
                    UPDATE program_control_projections
                    SET control_digest = ? WHERE program_id = ?
                    """,
                    ("0" * 64, "p-1"),
                )
                with self.assertRaises(IntegrityViolation):
                    operator.show("p-1")

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "capital.db"
            with LocalProgramOperator.open(database) as operator:
                operator.create(program_id="p-1", objective="schema integrity")
            connection = sqlite3.connect(database)
            try:
                connection.execute("DROP TABLE program_control_history")
                connection.commit()
            finally:
                connection.close()
            with self.assertRaises(IntegrityViolation):
                LocalProgramOperator.open(database)

    def test_reconciliation_presentation_is_scoped_to_the_correct_program(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "capital.db"
            with ProgramRepository(database) as programs:
                for program_id in ("p-1", "p-2"):
                    programs.create(Program(program_id, 0, "reconciliation"))
                    programs.transition(
                        program_id,
                        ProgramStatus.ACTIVE,
                        expected_revision=0,
                    )

                journal = OperationJournal(programs)
                resolution = CapabilityResolution(
                    "req-1",
                    "workspace.write",
                    0,
                    {"path": "notes.txt", "content": "updated"},
                    ResolvedEffect(
                        "file",
                        "notes.txt",
                        EffectClass.MODIFY,
                        {"path": "notes.txt"},
                    ),
                )
                operation = journal.create_intent(
                    program_id="p-1",
                    actor_id="a-1",
                    resolution=resolution,
                    authority_receipt_ref="authority-1",
                )
                journal.mark_admitted(operation.operation_id)
                journal.mark_running(operation.operation_id)
                journal.finish(
                    operation.operation_id,
                    ExecutionObservation(
                        ExecutionOutcome.TIMED_OUT,
                        EffectStatus.INDETERMINATE,
                        {},
                        error_code="timeout",
                    ),
                )

            with LocalProgramOperator.open(database) as operator:
                first = operator.show("p-1")
                second = operator.show("p-2")
                self.assertEqual(first["lifecycle"]["execution_state"], "reconciling")
                self.assertEqual(
                    first["lifecycle"]["pending_reconciliation_refs"],
                    (operation.operation_id,),
                )
                self.assertEqual(second["lifecycle"]["execution_state"], "running")
                self.assertEqual(second["lifecycle"]["pending_reconciliation_refs"], ())

    def test_cli_pause_and_resume_use_the_same_durable_operator(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "capital.db"
            created = _run_cli(
                database,
                "create",
                "--program-id",
                "p-1",
                "--objective",
                "cli lifecycle",
            )
            self.assertEqual(created.returncode, 0, created.stderr)
            started = _run_cli(
                database,
                "start",
                "p-1",
                "--expected-revision",
                "0",
            )
            self.assertEqual(started.returncode, 0, started.stderr)

            paused = _run_cli(
                database,
                "pause",
                "p-1",
                "--expected-revision",
                "1",
                "--expected-control-revision",
                "0",
            )
            self.assertEqual(paused.returncode, 0, paused.stderr)
            paused_json = json.loads(paused.stdout)
            self.assertEqual(paused_json["lifecycle"]["execution_state"], "paused")
            self.assertEqual(paused_json["control"]["revision"], 1)

            resumed = _run_cli(
                database,
                "resume",
                "p-1",
                "--expected-revision",
                "1",
                "--expected-control-revision",
                "1",
            )
            self.assertEqual(resumed.returncode, 0, resumed.stderr)
            resumed_json = json.loads(resumed.stdout)
            self.assertEqual(resumed_json["lifecycle"]["execution_state"], "running")
            self.assertEqual(resumed_json["control"]["revision"], 2)

            shown = _run_cli(database, "show", "p-1")
            self.assertEqual(shown.returncode, 0, shown.stderr)
            self.assertEqual(json.loads(shown.stdout), resumed_json)


if __name__ == "__main__":
    unittest.main()
