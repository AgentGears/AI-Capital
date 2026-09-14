from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from ai_capital.kernel.actor_store import ActorRepository
from ai_capital.kernel.authority import AuthorityEngine, PolicySnapshot
from ai_capital.kernel.authority_store import AuthorityRepository
from ai_capital.kernel.builtin_capabilities import install_builtin_capabilities
from ai_capital.kernel.capability_broker import CapabilityBroker, CapabilityHandlerRegistry
from ai_capital.kernel.capability_store import CapabilityRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import (
    EffectClass,
    EffectStatus,
    ExecutionOutcome,
    ProgramStatus,
    ReconciliationStatus,
)
from ai_capital.kernel.models import Actor, CapabilityRequest, Grant, Program
from ai_capital.kernel.operation_journal import OperationJournal
from ai_capital.product.program_operator import LocalProgramOperator


NOW = "2026-09-14T00:00:00Z"


class RestartFixture:
    def __init__(self, directory: str, *, capability_id: str):
        self.database = Path(directory) / "restart.db"
        self.programs = ProgramRepository(self.database)
        self.programs.create(Program("p-1", 0, "H2.7 restart matrix"))
        self.programs.transition("p-1", ProgramStatus.ACTIVE, expected_revision=0)
        self.actors = ActorRepository(self.programs)
        self.actors.register(Actor("a-1", 0, "worker", "binding-a"))
        self.capabilities = CapabilityRepository(self.programs)
        self.handlers = CapabilityHandlerRegistry()
        install_builtin_capabilities(self.capabilities, self.handlers)
        self.broker = CapabilityBroker(self.capabilities, self.handlers)
        self.authority_store = AuthorityRepository(self.programs)
        self.authority_store.install_policy(PolicySnapshot(0, (), (), NOW))
        effect_ceiling = (
            EffectClass.MODIFY
            if capability_id == "workspace.write"
            else EffectClass.OBSERVE
        )
        self.authority_store.issue_grant(
            Grant(
                "g-1",
                "actor:a-1",
                (capability_id,),
                ("*",),
                effect_ceiling,
                (),
                NOW,
                None,
                0,
            )
        )
        self.authority = AuthorityEngine(
            self.programs,
            self.actors,
            self.capabilities,
            self.authority_store,
        )
        self.journal = OperationJournal(self.programs)
        self.capability_id = capability_id

    def authorize(self, request_id: str):
        snapshot = self.broker.snapshot((self.capability_id,))
        arguments = (
            {"path": "notes.txt", "content": "updated"}
            if self.capability_id == "workspace.write"
            else {"path": "notes.txt"}
        )
        resolution = self.broker.resolve(
            CapabilityRequest(request_id, self.capability_id, arguments, 0),
            snapshot=snapshot,
        )
        context = self.authority.decide(
            program_id="p-1",
            actor_id="a-1",
            resolution=resolution,
        )
        receipt = self.authority.issue_execution_authority(
            decision_id=context.decision.decision_id
        )
        operation = self.journal.create_intent(
            program_id="p-1",
            actor_id="a-1",
            resolution=resolution,
            authority_receipt_ref=receipt.receipt_id,
        )
        return operation, receipt

    def close(self) -> None:
        self.programs.close()


class H2ReliabilityRestartMatrixTests(unittest.TestCase):
    def _recover(self, database: Path):
        with ProgramRepository(database) as programs:
            journal = OperationJournal(programs)
            recovered = journal.recover_interrupted()
            return recovered, journal

    def test_restart_before_admission_records_absent_mutating_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = RestartFixture(directory, capability_id="workspace.write")
            operation, _ = fixture.authorize("req-before-admission")
            database = fixture.database
            fixture.close()

            with ProgramRepository(database) as programs:
                journal = OperationJournal(programs)
                journal.recover_interrupted()
                recovered = journal.get(operation.operation_id)
                receipt = journal.execution_receipt(operation.operation_id)
            self.assertIs(recovered.execution_outcome, ExecutionOutcome.FAILED)
            self.assertIs(recovered.effect_status, EffectStatus.ABSENT)
            self.assertIs(
                recovered.reconciliation_status,
                ReconciliationStatus.NOT_REQUIRED,
            )
            self.assertEqual(receipt.error_code, "host_interrupted_before_admission")

    def test_restart_after_admission_before_dispatch_records_absent_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = RestartFixture(directory, capability_id="workspace.write")
            operation, authority = fixture.authorize("req-after-admission")
            fixture.authority.consume_execution_authority(receipt_id=authority.receipt_id)
            fixture.journal.mark_admitted(operation.operation_id)
            database = fixture.database
            fixture.close()

            with ProgramRepository(database) as programs:
                journal = OperationJournal(programs)
                journal.recover_interrupted()
                recovered = journal.get(operation.operation_id)
                receipt = journal.execution_receipt(operation.operation_id)
            self.assertIs(recovered.execution_outcome, ExecutionOutcome.FAILED)
            self.assertIs(recovered.effect_status, EffectStatus.ABSENT)
            self.assertIs(
                recovered.reconciliation_status,
                ReconciliationStatus.NOT_REQUIRED,
            )
            self.assertEqual(
                receipt.error_code,
                "host_interrupted_after_admission_before_dispatch",
            )

    def test_restart_running_observation_never_requires_reconciliation(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = RestartFixture(directory, capability_id="workspace.read")
            operation, authority = fixture.authorize("req-running-observe")
            fixture.authority.consume_execution_authority(receipt_id=authority.receipt_id)
            fixture.journal.mark_admitted(operation.operation_id)
            fixture.journal.mark_running(operation.operation_id)
            database = fixture.database
            fixture.close()

            with ProgramRepository(database) as programs:
                journal = OperationJournal(programs)
                journal.recover_interrupted()
                recovered = journal.get(operation.operation_id)
                receipt = journal.execution_receipt(operation.operation_id)
            self.assertIs(recovered.execution_outcome, ExecutionOutcome.FAILED)
            self.assertIs(recovered.effect_status, EffectStatus.NOT_APPLICABLE)
            self.assertIs(
                recovered.reconciliation_status,
                ReconciliationStatus.NOT_REQUIRED,
            )
            self.assertEqual(
                receipt.error_code,
                "host_interrupted_after_dispatch_boundary",
            )

    def test_restart_running_mutation_surfaces_reconciliation_after_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = RestartFixture(directory, capability_id="workspace.write")
            operation, authority = fixture.authorize("req-running-mutate")
            fixture.authority.consume_execution_authority(receipt_id=authority.receipt_id)
            fixture.journal.mark_admitted(operation.operation_id)
            fixture.journal.mark_running(operation.operation_id)
            database = fixture.database
            fixture.close()

            with ProgramRepository(database) as programs:
                journal = OperationJournal(programs)
                journal.recover_interrupted()
                recovered = journal.get(operation.operation_id)
            self.assertIs(recovered.execution_outcome, ExecutionOutcome.FAILED)
            self.assertIs(recovered.effect_status, EffectStatus.INDETERMINATE)
            self.assertIs(
                recovered.reconciliation_status,
                ReconciliationStatus.PENDING,
            )

            with LocalProgramOperator.open(database) as operator:
                view = operator.show("p-1")
            lifecycle = view["lifecycle"]
            self.assertEqual(lifecycle["execution_state"], "reconciling")
            self.assertEqual(
                lifecycle["reason_code"],
                "operation_reconciliation_required",
            )
            self.assertEqual(
                lifecycle["pending_reconciliation_refs"],
                [operation.operation_id],
            )


if __name__ == "__main__":
    unittest.main()
