from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest

from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import (
    EffectClass,
    EffectStatus,
    ExecutionOutcome,
    ProgramStatus,
)
from ai_capital.kernel.errors import InvalidRequest
from ai_capital.kernel.models import CapabilityResolution, Program, ResolvedEffect
from ai_capital.kernel.operation_journal import ExecutionObservation, OperationJournal
from ai_capital.kernel.serialization import canonical_digest, canonical_json
from ai_capital.product import LocalWorkspaceOperator


class H2ProgramBundleOperationAuthTests(unittest.TestCase):
    def _bundle_with_finished_operation(self, root: Path) -> bytes:
        database = root / "source.db"
        workspace = root / "workspace"
        workspace.mkdir()
        (workspace / "result.txt").write_bytes(b"portable operation result\n")

        with ProgramRepository(database) as programs:
            programs.create(Program("p-1", 0, "portable Operation audit"))
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
            journal.finish(
                operation.operation_id,
                ExecutionObservation(
                    ExecutionOutcome.SUCCEEDED,
                    EffectStatus.CONFIRMED,
                    {"bytes_written": 7},
                    backend_receipt_ref="backend-1",
                ),
            )
            programs._commit_change(
                program_id="p-1",
                expected_revision=1,
                event_type="program.revised",
                mutate=lambda current: replace(
                    current,
                    revision=current.revision + 1,
                    operation_refs=current.operation_refs + (operation.operation_id,),
                ),
                event_id=None,
                occurred_at=None,
                recorded_at=None,
            )

        with LocalWorkspaceOperator.open(database) as operator:
            snapshot = operator.snapshot("p-1")
            return operator.export_bundle(
                "p-1",
                snapshot["snapshot"]["snapshot_id"],
            )

    def _assert_import_rejected(self, root: Path, content: bytes) -> None:
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
                operator.import_bundle(content)

    @staticmethod
    def _reidentify(envelope: dict) -> bytes:
        envelope["bundle_id"] = "program-bundle:" + canonical_digest(envelope["payload"])
        return canonical_json(envelope).encode("utf-8")

    def test_recomputed_bundle_with_tampered_execution_receipt_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            envelope = json.loads(self._bundle_with_finished_operation(root))
            receipt = envelope["payload"]["audit"]["operations"][0]["receipts"][0]
            receipt["output"] = {"bytes_written": 999}
            self._assert_import_rejected(root, self._reidentify(envelope))

    def test_recomputed_bundle_with_tampered_operation_event_anchor_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            envelope = json.loads(self._bundle_with_finished_operation(root))
            event = envelope["payload"]["audit"]["operations"][0]["events"][-1]
            event["digest"] = "0" * 64
            self._assert_import_rejected(root, self._reidentify(envelope))


if __name__ == "__main__":
    unittest.main()
