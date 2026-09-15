from pathlib import Path


def replace_once(path: str, old: str, new: str) -> None:
    p = Path(path)
    text = p.read_text()
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"expected one match in {path}, found {count}")
    p.write_text(text.replace(old, new, 1))


# 1. Bind each execution-authority receipt to at most one durable Operation intent.
path = "src/ai_capital/kernel/operation_journal.py"
replace_once(path, "_COMPONENT_SCHEMA_VERSION = 3", "_COMPONENT_SCHEMA_VERSION = 4")
replace_once(
    path,
    '''                self._host_store._db.execute(
                    """
                    CREATE INDEX operations_program
                        ON operation_projections(program_id, operation_id)
                    """
                )
                self._host_store._db.execute(
                    """
                    CREATE TABLE operation_receipts (
''',
    '''                self._host_store._db.execute(
                    """
                    CREATE INDEX operations_program
                        ON operation_projections(program_id, operation_id)
                    """
                )
                self._host_store._db.execute(
                    """
                    CREATE UNIQUE INDEX operations_authority_receipt
                        ON operation_projections(authority_receipt_ref)
                    """
                )
                self._host_store._db.execute(
                    """
                    CREATE TABLE operation_receipts (
''',
)
replace_once(
    path,
    '''                self._host_store._db.execute(
                    "UPDATE component_schema SET version = ? WHERE component = ?",
                    (_COMPONENT_SCHEMA_VERSION, _COMPONENT),
                )
                version = _COMPONENT_SCHEMA_VERSION

            if version != _COMPONENT_SCHEMA_VERSION:
''',
    '''                self._host_store._db.execute(
                    "UPDATE component_schema SET version = ? WHERE component = ?",
                    (3, _COMPONENT),
                )
                version = 3

            if version == 3:
                duplicate_authority = self._host_store._db.execute(
                    """
                    SELECT authority_receipt_ref
                    FROM operation_projections
                    GROUP BY authority_receipt_ref
                    HAVING COUNT(*) > 1
                    LIMIT 1
                    """
                ).fetchone()
                if duplicate_authority is not None:
                    raise IntegrityViolation(
                        "execution authority is bound to multiple Operation intents"
                    )
                self._host_store._db.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS operations_authority_receipt
                        ON operation_projections(authority_receipt_ref)
                    """
                )
                self._host_store._db.execute(
                    "UPDATE component_schema SET version = ? WHERE component = ?",
                    (_COMPONENT_SCHEMA_VERSION, _COMPONENT),
                )
                version = _COMPONENT_SCHEMA_VERSION

            if version != _COMPONENT_SCHEMA_VERSION:
''',
)
replace_once(
    path,
    '''        try:
            with self._host_store._transaction():
                event = self._append_event(
''',
    '''        try:
            with self._host_store._transaction():
                existing_authority = self._host_store._db.execute(
                    """
                    SELECT operation_id FROM operation_projections
                    WHERE authority_receipt_ref = ? LIMIT 1
                    """,
                    (authority_receipt_ref,),
                ).fetchone()
                if existing_authority is not None:
                    raise PersistenceConflict(
                        "execution authority already has a durable Operation intent"
                    )
                event = self._append_event(
''',
)

# 2. Repair Program operation_refs for every durable orphan Operation on product reopen.
path = "src/ai_capital/product/reliability.py"
replace_once(path, "from dataclasses import dataclass", "from dataclasses import dataclass, replace")
replace_once(
    path,
    "from ..kernel.events import utc_now\nfrom ..kernel.serialization import canonical_digest, canonical_json\n",
    "from ..kernel.events import utc_now\nfrom ..kernel.models import Operation\nfrom ..kernel.operation_journal import OperationJournal\nfrom ..kernel.serialization import canonical_digest, canonical_json\n",
)
p = Path(path)
text = p.read_text()
if "def link_durable_operations(" in text:
    raise SystemExit("link helper already present")
addition = '''


def link_durable_operations(
    programs: ProgramRepository,
    operations: OperationJournal,
) -> tuple[Operation, ...]:
    """Idempotently repair Program links for authenticated durable Operations."""
    rows = programs._db.execute(
        "SELECT operation_id FROM operation_projections ORDER BY operation_id"
    ).fetchall()
    linked: list[Operation] = []
    for row in rows:
        operation = operations.get(str(row["operation_id"]))
        current = programs.get(operation.program_id)
        if operation.operation_id in current.operation_refs:
            continue
        programs._commit_change(
            program_id=current.program_id,
            expected_revision=current.revision,
            event_type="program.revised",
            mutate=lambda program, operation_id=operation.operation_id: replace(
                program,
                revision=program.revision + 1,
                operation_refs=program.operation_refs + (operation_id,),
            ),
            event_id=None,
            occurred_at=None,
            recorded_at=None,
        )
        linked.append(operation)
    return tuple(linked)
'''
p.write_text(text.rstrip() + addition + "\n")

path = "src/ai_capital/product/capability_operator.py"
replace_once(
    path,
    "from .reliability import ProductRequestRecord, ProductRequestRepository",
    '''from .reliability import (
    ProductRequestRecord,
    ProductRequestRepository,
    link_durable_operations,
)''',
)
replace_once(
    path,
    '''        self._journal = OperationJournal(programs)
        if owns_repository:
            self._journal.recover_interrupted()
        self._host = OperationHost(self._journal, self._authority)
''',
    '''        self._journal = OperationJournal(programs)
        if owns_repository:
            self._journal.recover_interrupted()
            link_durable_operations(programs, self._journal)
        self._host = OperationHost(self._journal, self._authority)
''',
)

path = "src/ai_capital/product/program_operator.py"
replace_once(
    path,
    "from .audit_operator import LocalAuditOperator\n",
    "from .audit_operator import LocalAuditOperator\nfrom .reliability import link_durable_operations\n",
)
replace_once(
    path,
    '''        self._operations = OperationJournal(programs)
        if owns_repository:
            self._operations.recover_interrupted()
        self._audit: LocalAuditOperator | None = None
''',
    '''        self._operations = OperationJournal(programs)
        if owns_repository:
            self._operations.recover_interrupted()
            link_durable_operations(programs, self._operations)
        self._audit: LocalAuditOperator | None = None
''',
)

# 3. Make Git observation execution-time profile helper- and protocol-inert.
path = "src/ai_capital/product/capability_executors.py"
replace_once(
    path,
    '''            safe_git = [
                "git",
                f"--git-dir={git_path}",
''',
    '''            safe_git = [
                "git",
                "--no-pager",
                f"--git-dir={git_path}",
''',
)
replace_once(
    path,
    '''                    "status",
                    "--short",
                    "--branch",
                    "--ignore-submodules=all",
''',
    '''                    "status",
                    "--short",
                    "--branch",
                    "--no-ahead-behind",
                    "--ignore-submodules=all",
''',
)
replace_once(
    path,
    '''                "diff": [
                    *safe_git,
                    "diff",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--ignore-submodules=all",
                ],
''',
    '''                "diff": [
                    *safe_git,
                    "diff-files",
                    "--raw",
                    "--no-renames",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--ignore-submodules=all",
                ],
''',
)
replace_once(
    path,
    '''                        "GIT_CONFIG_NOSYSTEM": "1",
                        "GIT_ATTR_NOSYSTEM": "1",
                        "GIT_PAGER": "",
                        "PAGER": "",
                        "GIT_OPTIONAL_LOCKS": "0",
                        "GIT_TERMINAL_PROMPT": "0",
''',
    '''                        "GIT_CONFIG_NOSYSTEM": "1",
                        "GIT_CONFIG_SYSTEM": os.devnull,
                        "GIT_CONFIG_GLOBAL": os.devnull,
                        "GIT_ATTR_NOSYSTEM": "1",
                        "GIT_PAGER": "",
                        "PAGER": "",
                        "GIT_OPTIONAL_LOCKS": "0",
                        "GIT_TERMINAL_PROMPT": "0",
                        "GIT_NO_LAZY_FETCH": "1",
                        "GIT_ALLOW_PROTOCOL": "",
                        "GIT_PROTOCOL_FROM_USER": "0",
                        "GIT_NO_REPLACE_OBJECTS": "1",
                        "GIT_COMMON_DIR": git_path,
''',
)
replace_once(
    path,
    '''                completed = run_bounded_process(
                    argv,
                    cwd=repository_path,
                    executable=executable,
                    env=environment,
                    timeout_seconds=_COMMAND_TIMEOUT_SECONDS,
                    max_output_bytes=_MAX_OBSERVATION_BYTES,
                    pass_fds=(root_fd, repository_fd, git_fd),
                )
        finally:
''',
    '''                completed = run_bounded_process(
                    argv,
                    cwd=repository_path,
                    executable=executable,
                    env=environment,
                    timeout_seconds=_COMMAND_TIMEOUT_SECONDS,
                    max_output_bytes=_MAX_OBSERVATION_BYTES,
                    pass_fds=(root_fd, repository_fd, git_fd),
                )
                validate_git_directory_fd(git_fd)
        finally:
''',
)

# Restart regressions for authority binding and Program-link repair.
path = "tests/test_h2_reliability_restart_matrix.py"
replace_once(
    path,
    "from ai_capital.kernel.models import Actor, CapabilityRequest, Grant, Program\n",
    "from ai_capital.kernel.errors import PersistenceConflict\nfrom ai_capital.kernel.models import Actor, CapabilityRequest, Grant, Program\n",
)
p = Path(path)
text = p.read_text()
marker = "    def test_restart_after_admission_before_dispatch_records_absent_effect(self):\n"
if marker not in text:
    raise SystemExit("restart insertion marker missing")
test = '''    def test_restart_before_admission_cannot_reuse_execution_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = RestartFixture(directory, capability_id="workspace.write")
            operation, authority = fixture.authorize("req-authority-once")
            resolution = fixture.journal.resolution(operation.operation_id)
            database = fixture.database
            fixture.close()

            with ProgramRepository(database) as programs:
                journal = OperationJournal(programs)
                journal.recover_interrupted()
                with self.assertRaisesRegex(
                    PersistenceConflict,
                    "execution authority already has a durable Operation intent",
                ):
                    journal.create_intent(
                        program_id="p-1",
                        actor_id="a-1",
                        resolution=resolution,
                        authority_receipt_ref=authority.receipt_id,
                    )
                operation_count = int(
                    programs._db.execute(
                        "SELECT COUNT(*) FROM operation_projections"
                    ).fetchone()[0]
                )
            self.assertEqual(operation_count, 1)

'''
p.write_text(text.replace(marker, test + marker, 1))
replace_once(
    path,
    '''            with LocalProgramOperator.open(database) as operator:
                view = operator.show("p-1")
                recovered = operator._operations.get(operation.operation_id)
            self.assertIs(recovered.execution_outcome, ExecutionOutcome.FAILED)
''',
    '''            with LocalProgramOperator.open(database) as operator:
                view = operator.show("p-1")
                recovered = operator._operations.get(operation.operation_id)
                linked_program = operator._programs.get("p-1")
                linked_revision = linked_program.revision
                self.assertIn(operation.operation_id, linked_program.operation_refs)
            with LocalProgramOperator.open(database) as operator:
                reopened = operator._programs.get("p-1")
                self.assertEqual(reopened.revision, linked_revision)
                self.assertEqual(
                    reopened.operation_refs.count(operation.operation_id),
                    1,
                )
            self.assertIs(recovered.execution_outcome, ExecutionOutcome.FAILED)
''',
)

# Regression: mutate repository config only after descriptor validation and prove no helper effect.
path = "tests/test_h2_reliability_review.py"
replace_once(path, "import os\nimport sys\n", "import os\nimport shutil\nimport subprocess\nimport sys\n")
replace_once(
    path,
    "from ai_capital.product import rooted_io\nfrom ai_capital.product.process_observation import run_bounded_process\n",
    '''from ai_capital.product import rooted_io
from ai_capital.product.git_repository_guard import (
    validate_git_directory_fd as real_validate_git_directory_fd,
)
from ai_capital.product.process_observation import run_bounded_process
''',
)
p = Path(path)
text = p.read_text()
marker = "    def test_approved_request_recovers_issued_authority_before_operation_after_restart(self):\n"
if marker not in text:
    raise SystemExit("review insertion marker missing")
test = '''    @unittest.skipIf(shutil.which("git") is None, "git is unavailable")
    def test_git_observe_blocks_helper_added_after_metadata_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, workspace, artifacts = self._fixture(root)
            subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
            (workspace / ".gitattributes").write_text("*.txt filter=late\n")
            tracked = workspace / "tracked.txt"
            tracked.write_text("base\n")
            subprocess.run(
                ["git", "add", ".gitattributes", "tracked.txt"],
                cwd=workspace,
                check=True,
            )
            tracked.write_text("changed\n")
            sentinel = root / "helper-ran"
            config = workspace / ".git" / "config"
            validations = 0

            def validate_then_mutate(descriptor: int) -> None:
                nonlocal validations
                validations += 1
                real_validate_git_directory_fd(descriptor)
                if validations == 1:
                    with config.open("a", encoding="utf-8") as stream:
                        stream.write(
                            f'\n[filter "late"]\n\tclean = touch {sentinel}\n'
                        )

            with self._open(database, workspace, artifacts) as operator:
                operator.grant(
                    actor_id="a-1",
                    capability_id="git.observe",
                    resource_scope=(".",),
                )
                with patch(
                    "ai_capital.product.capability_executors.validate_git_directory_fd",
                    side_effect=validate_then_mutate,
                ):
                    result = operator.invoke(
                        program_id="p-1",
                        actor_id="a-1",
                        capability_id="git.observe",
                        arguments={"path": ".", "operation": "diff"},
                    )
            self.assertGreaterEqual(validations, 2)
            self.assertEqual(result["operation"]["execution_outcome"], "failed")
            self.assertEqual(result["operation"]["effect_status"], "not_applicable")
            self.assertFalse(sentinel.exists())

'''
p.write_text(text.replace(marker, test + marker, 1))
