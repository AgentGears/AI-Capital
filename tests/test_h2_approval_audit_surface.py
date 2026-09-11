from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_capital.kernel.actor_store import ActorRepository
from ai_capital.kernel.authority import AuthorityEngine, PolicySnapshot
from ai_capital.kernel.authority_store import AuthorityRepository
from ai_capital.kernel.builtin_capabilities import install_builtin_capabilities
from ai_capital.kernel.capability_broker import CapabilityBroker, CapabilityHandlerRegistry
from ai_capital.kernel.capability_store import CapabilityRepository
from ai_capital.kernel.claim_store import ClaimRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import (
    EffectClass,
    EffectStatus,
    ExecutionOutcome,
    ProgramStatus,
    RiskClass,
    VerificationResult,
)
from ai_capital.kernel.errors import (
    ApprovalInvalid,
    IntegrityViolation,
    StaleProgramRevision,
)
from ai_capital.kernel.evidence_store import EvidenceRepository
from ai_capital.kernel.models import (
    Actor,
    CapabilityRequest,
    CapabilityResolution,
    Grant,
    Program,
    ResolvedEffect,
)
from ai_capital.kernel.operation_journal import ExecutionObservation, OperationJournal
from ai_capital.kernel.verification import (
    VerificationObservation,
    VerificationRepository,
)
from ai_capital.product import LocalProgramOperator


ROOT = Path(__file__).resolve().parents[1]
NOW = "2026-09-11T00:00:00Z"


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


class PassingVerifier:
    def verify(self, contract, program, evidence_refs):
        return VerificationObservation(VerificationResult.PASS, "criteria_verified")


def _create_ask(database: Path, *, program_id: str = "p-1"):
    with ProgramRepository(database) as programs:
        programs.create(Program(program_id, 0, "approval audit"))
        programs.transition(program_id, ProgramStatus.ACTIVE, expected_revision=0)

        actors = ActorRepository(programs)
        actors.register(Actor("a-1", 0, "worker", "binding-a"))

        capabilities = CapabilityRepository(programs)
        handlers = CapabilityHandlerRegistry()
        install_builtin_capabilities(capabilities, handlers)
        broker = CapabilityBroker(capabilities, handlers)

        authority_store = AuthorityRepository(programs)
        authority_store.install_policy(
            PolicySnapshot(0, (RiskClass.MEDIUM,), (), NOW)
        )
        authority_store.issue_grant(
            Grant(
                "g-1",
                "actor:a-1",
                ("workspace.write",),
                ("*",),
                EffectClass.MODIFY,
                (),
                NOW,
                None,
                0,
            )
        )
        resolution = broker.resolve(
            CapabilityRequest(
                "req-1",
                "workspace.write",
                {"path": "notes.txt", "content": "updated"},
                0,
            ),
            snapshot=broker.snapshot(("workspace.write",)),
        )
        engine = AuthorityEngine(
            programs,
            actors,
            capabilities,
            authority_store,
        )
        decision = engine.decide(
            program_id=program_id,
            actor_id="a-1",
            resolution=resolution,
        )
        return decision.decision.decision_id, resolution


class H2ApprovalAuditSurfaceTests(unittest.TestCase):
    def test_asks_and_approval_survive_restart_and_consumption(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "capital.db"
            decision_id, _ = _create_ask(database)

            with LocalProgramOperator.open(database) as operator:
                asks = operator.asks("p-1")
                self.assertEqual(len(asks), 1)
                self.assertEqual(asks[0]["decision"]["decision_id"], decision_id)
                self.assertEqual(asks[0]["approval_state"], "awaiting_approval")
                self.assertEqual(asks[0]["currentness"], "current")
                self.assertEqual(asks[0]["decision_event"]["event_type"], "authority.decided")

                approved = operator.approve(decision_id)
                self.assertEqual(approved["approval_state"], "approved")
                approval_id = approved["approval"]["receipt"]["approval_id"]
                with self.assertRaises(ApprovalInvalid):
                    operator.approve(decision_id)

            with LocalProgramOperator.open(database) as restarted:
                approved_after_restart = restarted.asks("p-1")[0]
                self.assertEqual(approved_after_restart, approved)

            with ProgramRepository(database) as programs:
                actors = ActorRepository(programs)
                capabilities = CapabilityRepository(programs)
                authority_store = AuthorityRepository(programs)
                engine = AuthorityEngine(
                    programs,
                    actors,
                    capabilities,
                    authority_store,
                )
                execution = engine.issue_execution_authority(
                    decision_id=decision_id,
                    approval_id=approval_id,
                )

            with LocalProgramOperator.open(database) as restarted_again:
                consumed = restarted_again.asks("p-1")[0]
                self.assertEqual(consumed["approval_state"], "consumed")
                self.assertIsNotNone(consumed["approval"]["consumed_at"])
                self.assertEqual(
                    consumed["approval"]["consumed_event"]["event_type"],
                    "approval.consumed",
                )
                self.assertEqual(
                    consumed["execution_authority"]["receipt"]["receipt_id"],
                    execution.receipt_id,
                )
                self.assertIsNone(consumed["execution_authority"]["consumed_at"])

    def test_asks_are_program_scoped_and_stale_approval_fails_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "capital.db"
            decision_id, _ = _create_ask(database)
            with ProgramRepository(database) as programs:
                programs.create(Program("p-2", 0, "other program"))
                programs.transition("p-2", ProgramStatus.ACTIVE, expected_revision=0)
                programs.transition("p-1", ProgramStatus.BLOCKED, expected_revision=1)

            with LocalProgramOperator.open(database) as operator:
                self.assertEqual(operator.asks("p-2"), ())
                stale = operator.asks("p-1")[0]
                self.assertEqual(stale["currentness"], "stale")
                self.assertEqual(stale["stale_reason"]["code"], "StaleProgramRevision")
                with self.assertRaises(StaleProgramRevision):
                    operator.approve(decision_id)
                count = operator._programs._db.execute(
                    "SELECT COUNT(*) FROM approval_receipts WHERE decision_id = ?",
                    (decision_id,),
                ).fetchone()
                self.assertEqual(int(count[0]), 0)

    def test_cli_asks_and_approve_use_the_same_host_approval_path(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "capital.db"
            decision_id, _ = _create_ask(database)

            asks = _run_cli(database, "asks", "p-1")
            self.assertEqual(asks.returncode, 0, asks.stderr)
            ask_json = json.loads(asks.stdout)
            self.assertEqual(len(ask_json), 1)
            self.assertEqual(ask_json[0]["approval_state"], "awaiting_approval")

            approved = _run_cli(database, "approve", decision_id)
            self.assertEqual(approved.returncode, 0, approved.stderr)
            approved_json = json.loads(approved.stdout)
            self.assertEqual(approved_json["approval_state"], "approved")

            duplicate = _run_cli(database, "approve", decision_id)
            self.assertEqual(duplicate.returncode, 2)
            self.assertEqual(json.loads(duplicate.stderr)["error"]["code"], "ApprovalInvalid")

    def test_operation_audit_binds_projection_receipts_and_semantic_events(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "capital.db"
            with ProgramRepository(database) as programs:
                programs.create(Program("p-1", 0, "operation audit"))
                programs.transition("p-1", ProgramStatus.ACTIVE, expected_revision=0)
                journal = OperationJournal(programs)
                resolution = CapabilityResolution(
                    "req-1",
                    "workspace.write",
                    0,
                    {"path": "notes.txt", "content": "updated"},
                    ResolvedEffect(
                        "workspace_path",
                        "notes.txt",
                        EffectClass.MODIFY,
                        {"content": "updated"},
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
                finished = journal.finish(
                    operation.operation_id,
                    ExecutionObservation(
                        ExecutionOutcome.SUCCEEDED,
                        EffectStatus.CONFIRMED,
                        {"bytes_written": 7},
                        backend_receipt_ref="backend-1",
                    ),
                )

            with LocalProgramOperator.open(database) as operator:
                audit = operator.audit_operation(operation.operation_id)
                self.assertEqual(audit["operation"]["operation_id"], operation.operation_id)
                self.assertEqual(
                    audit["operation"]["receipt_refs"],
                    list(finished.receipt_refs),
                )
                self.assertEqual(len(audit["receipts"]), 1)
                self.assertEqual(
                    [event["event_type"] for event in audit["events"]],
                    [
                        "operation.requested",
                        "operation.admitted",
                        "operation.started",
                        "operation.finished",
                    ],
                )
                operator._programs._db.execute(
                    """
                    UPDATE operation_receipts SET receipt_digest = ?
                    WHERE operation_id = ?
                    """,
                    ("0" * 64, operation.operation_id),
                )
                with self.assertRaises(IntegrityViolation):
                    operator.audit_operation(operation.operation_id)

    def test_evidence_audit_authenticates_artifact_and_admission_event(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "capital.db"
            with ProgramRepository(database) as programs:
                evidence_store = EvidenceRepository(programs)
                evidence = evidence_store.admit(
                    content=b"authenticated evidence bytes",
                    source_class="test_fixture",
                    observed_at=NOW,
                    provenance=("fixture:source",),
                    trust_class="direct",
                    currentness="current",
                )

            with LocalProgramOperator.open(database) as operator:
                audit = operator.audit_evidence(evidence.evidence_id)
                self.assertEqual(audit["evidence"]["evidence_id"], evidence.evidence_id)
                self.assertEqual(audit["artifact"]["digest"], evidence.digest)
                self.assertEqual(audit["artifact"]["byte_length"], len(b"authenticated evidence bytes"))
                self.assertEqual(audit["event"]["event_type"], "evidence.admitted")
                operator._programs._db.execute(
                    """
                    UPDATE events SET event_digest = ?
                    WHERE event_id = (
                        SELECT admitted_event_id FROM evidence_records WHERE evidence_id = ?
                    )
                    """,
                    ("0" * 64, evidence.evidence_id),
                )
                with self.assertRaises(IntegrityViolation):
                    operator.audit_evidence(evidence.evidence_id)

    def test_verification_audit_reports_current_then_stale_with_exact_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "capital.db"
            with ProgramRepository(database) as programs:
                programs.create(
                    Program(
                        "p-v",
                        0,
                        "verification audit",
                        success_criteria=("done",),
                    )
                )
                programs.transition("p-v", ProgramStatus.ACTIVE, expected_revision=0)
                evidence = EvidenceRepository(programs)
                claims = ClaimRepository(programs, evidence)
                verifications = VerificationRepository(programs, claims)
                contract = verifications.register_contract(
                    program_id="p-v",
                    success_criteria=("done",),
                )
                programs.transition(
                    "p-v",
                    ProgramStatus.COMPLETION_PENDING,
                    expected_revision=1,
                )
                verification = verifications.run(
                    contract.contract_id,
                    expected_program_revision=2,
                    verifier=PassingVerifier(),
                )

            with LocalProgramOperator.open(database) as operator:
                current = operator.audit_verification(verification.verification_id)
                self.assertEqual(current["currentness"], "current")
                self.assertEqual(current["rationale_code"], "criteria_verified")
                self.assertEqual(current["event"]["event_type"], "verification.recorded")
                operator._programs.transition(
                    "p-v",
                    ProgramStatus.BLOCKED,
                    expected_revision=2,
                )
                stale = operator.audit_verification(verification.verification_id)
                self.assertEqual(stale["currentness"], "stale")
                self.assertTrue(stale["stale_reason"])

    def test_verification_audit_fails_closed_on_event_corruption(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "capital.db"
            with ProgramRepository(database) as programs:
                programs.create(
                    Program(
                        "p-v",
                        0,
                        "verification integrity",
                        success_criteria=("done",),
                    )
                )
                programs.transition("p-v", ProgramStatus.ACTIVE, expected_revision=0)
                evidence = EvidenceRepository(programs)
                claims = ClaimRepository(programs, evidence)
                verifications = VerificationRepository(programs, claims)
                contract = verifications.register_contract(
                    program_id="p-v",
                    success_criteria=("done",),
                )
                programs.transition(
                    "p-v",
                    ProgramStatus.COMPLETION_PENDING,
                    expected_revision=1,
                )
                verification = verifications.run(
                    contract.contract_id,
                    expected_program_revision=2,
                    verifier=PassingVerifier(),
                )
                programs._db.execute(
                    """
                    UPDATE events SET event_digest = ?
                    WHERE event_id = (
                        SELECT event_id FROM verification_receipts
                        WHERE verification_id = ?
                    )
                    """,
                    ("0" * 64, verification.verification_id),
                )

            with LocalProgramOperator.open(database) as operator:
                with self.assertRaises(IntegrityViolation):
                    operator.audit_verification(verification.verification_id)


if __name__ == "__main__":
    unittest.main()
