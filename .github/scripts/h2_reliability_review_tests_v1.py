from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text()
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected one match in {path}, found {count}")
    p.write_text(text.replace(old, new, 1))


review_path = "tests/test_h2_reliability_review.py"
review_marker = '''    def _open(self, database: Path, workspace: Path, artifacts: Path):
        return LocalCapabilityOperator.open(database, workspace_root=workspace, artifact_root=artifacts)

'''
review_test = r'''    def test_concurrent_duplicate_process_is_serialized_by_product_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace, artifacts = self._fixture(root)
            arguments = {"path": "concurrent.txt", "content": "once\n"}
            contender_script = r"""
import sys
from pathlib import Path
from ai_capital.kernel.errors import PersistenceConflict
from ai_capital.product import LocalCapabilityOperator

database, workspace, artifacts = (Path(value) for value in sys.argv[1:4])
try:
    with LocalCapabilityOperator.open(
        database,
        workspace_root=workspace,
        artifact_root=artifacts,
    ) as operator:
        operator.invoke(
            program_id="p-1",
            actor_id="a-1",
            capability_id="workspace.write",
            arguments={"path": "concurrent.txt", "content": "once\n"},
            request_id="req-concurrent-process",
        )
except PersistenceConflict:
    raise SystemExit(0)
raise SystemExit(9)
"""
            with self._open(database, workspace, artifacts) as operator:
                operator.grant(
                    actor_id="a-1",
                    capability_id="workspace.write",
                    resource_scope=("concurrent.txt",),
                )
                contender = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        contender_script,
                        str(database),
                        str(workspace),
                        str(artifacts),
                    ],
                    env=dict(os.environ),
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                self.assertEqual(
                    contender.returncode,
                    0,
                    msg=f"stdout={contender.stdout!r} stderr={contender.stderr!r}",
                )
                first = operator.invoke(
                    program_id="p-1",
                    actor_id="a-1",
                    capability_id="workspace.write",
                    arguments=arguments,
                    request_id="req-concurrent-process",
                )
                operation_count = int(
                    operator._programs._db.execute(
                        "SELECT COUNT(*) FROM operation_projections"
                    ).fetchone()[0]
                )
            with self._open(database, workspace, artifacts) as operator:
                replay = operator.invoke(
                    program_id="p-1",
                    actor_id="a-1",
                    capability_id="workspace.write",
                    arguments=arguments,
                    request_id="req-concurrent-process",
                )
                replay_count = int(
                    operator._programs._db.execute(
                        "SELECT COUNT(*) FROM operation_projections"
                    ).fetchone()[0]
                )
            self.assertEqual(first, replay)
            self.assertEqual(operation_count, 1)
            self.assertEqual(replay_count, 1)
            self.assertEqual((workspace / "concurrent.txt").read_text(), "once\n")

'''
replace_once(review_path, review_marker, review_marker + review_test)

restart_path = "tests/test_h2_reliability_restart_matrix.py"
restart_marker = '''    def test_restart_running_mutation_surfaces_reconciliation_after_reopen(self):
'''
restart_test = '''    def test_restart_repairs_already_terminal_orphan_operation_link_once(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = RestartFixture(directory, capability_id="workspace.write")
            operation, authority = fixture.authorize("req-terminal-orphan")
            fixture.authority.consume_execution_authority(receipt_id=authority.receipt_id)
            fixture.journal.mark_admitted(operation.operation_id)
            terminal = fixture.journal.fail_before_dispatch(
                operation.operation_id,
                error_code="fixture_terminal_before_dispatch",
            )
            self.assertIs(terminal.execution_outcome, ExecutionOutcome.FAILED)
            database = fixture.database
            fixture.close()

            with LocalProgramOperator.open(database) as operator:
                linked = operator._programs.get("p-1")
                linked_revision = linked.revision
                self.assertEqual(
                    linked.operation_refs.count(operation.operation_id),
                    1,
                )
            with LocalProgramOperator.open(database) as operator:
                reopened = operator._programs.get("p-1")
                self.assertEqual(reopened.revision, linked_revision)
                self.assertEqual(
                    reopened.operation_refs.count(operation.operation_id),
                    1,
                )

'''
replace_once(restart_path, restart_marker, restart_test + restart_marker)
