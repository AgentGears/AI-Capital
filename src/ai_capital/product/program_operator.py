from __future__ import annotations

from pathlib import Path
from typing import Any

from ..kernel.durable_program import ProgramRepository
from ..kernel.enums import ProgramStatus
from ..kernel.errors import InvalidRequest, InvalidStateTransition, StaleProgramRevision
from ..kernel.models import Program
from ..kernel.operation_journal import OperationJournal
from ..kernel.program_control import ProgramControl, ProgramControlRepository
from ..kernel.serialization import to_canonical_data


class LocalProgramOperator:
    """Local product adapter over Host-owned durable Program authority."""

    def __init__(self, programs: ProgramRepository, *, owns_repository: bool = False):
        self._programs = programs
        self._controls = ProgramControlRepository(programs)
        self._operations = OperationJournal(programs)
        self._owns_repository = owns_repository
        self._closed = False

    @classmethod
    def open(cls, database_path: str | Path) -> "LocalProgramOperator":
        programs = ProgramRepository(database_path)
        try:
            return cls(programs, owns_repository=True)
        except Exception:
            programs.close()
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_repository:
            self._programs.close()

    def __enter__(self) -> "LocalProgramOperator":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise InvalidRequest("local Program operator is closed")

    @staticmethod
    def _require_text(value: str, *, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise InvalidRequest(f"{field} must be non-empty")
        return value

    def _lifecycle(self, program: Program, control: ProgramControl) -> dict[str, Any]:
        pending = tuple(
            operation
            for operation in self._operations.pending_reconciliation()
            if operation.program_id == program.program_id
        )
        pending_refs = tuple(operation.operation_id for operation in pending)

        if pending_refs:
            execution_state = "reconciling"
            reason_code = "operation_reconciliation_required"
        elif program.status is ProgramStatus.COMPLETED:
            execution_state = "completed"
            reason_code = "program_completed"
        elif program.status is ProgramStatus.CANCELLED:
            execution_state = "cancelled"
            reason_code = "program_cancelled"
        elif program.status is ProgramStatus.FAILED:
            execution_state = "failed"
            reason_code = "program_failed"
        elif program.status is ProgramStatus.BLOCKED:
            execution_state = "blocked"
            reason_code = "host_blocked"
        elif program.status is ProgramStatus.ACTIVE and control.paused:
            execution_state = "paused"
            reason_code = "user_paused"
        elif program.status is ProgramStatus.CREATED:
            execution_state = "waiting"
            reason_code = "awaiting_start"
        elif program.status is ProgramStatus.COMPLETION_PENDING:
            execution_state = "waiting"
            reason_code = "completion_pending"
        elif program.status is ProgramStatus.ACTIVE:
            execution_state = "running"
            reason_code = "program_active"
        else:
            raise InvalidStateTransition(
                f"Program lifecycle cannot be presented from {program.status.value}"
            )

        return {
            "program_status": program.status.value,
            "execution_state": execution_state,
            "reason_code": reason_code,
            "pending_reconciliation_refs": pending_refs,
        }

    def _view(self, program: Program) -> dict[str, Any]:
        self._controls.verify_integrity(program.program_id)
        events = self._programs.list_events(program.program_id)
        control = self._controls.get(program.program_id)
        return {
            "program": to_canonical_data(program),
            "event_count": len(events),
            "control": to_canonical_data(control),
            "lifecycle": self._lifecycle(program, control),
        }

    def create(
        self,
        *,
        program_id: str,
        objective: str,
        constraints: tuple[str, ...] = (),
        success_criteria: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        self._ensure_open()
        program_id = self._require_text(program_id, field="program_id")
        objective = self._require_text(objective, field="objective")
        for value in constraints:
            self._require_text(value, field="constraint")
        for value in success_criteria:
            self._require_text(value, field="success_criterion")
        program = self._programs.create(
            Program(
                program_id,
                0,
                objective,
                constraints=constraints,
                success_criteria=success_criteria,
            )
        )
        return self._view(program)

    def list(self) -> tuple[dict[str, Any], ...]:
        self._ensure_open()
        return tuple(self._view(program) for program in self._programs.list_programs())

    def show(self, program_id: str) -> dict[str, Any]:
        self._ensure_open()
        program_id = self._require_text(program_id, field="program_id")
        return self._view(self._programs.get(program_id))

    def start(self, program_id: str, *, expected_revision: int) -> dict[str, Any]:
        self._ensure_open()
        program_id = self._require_text(program_id, field="program_id")
        self._controls.verify_integrity(program_id)
        current = self._programs.get(program_id)
        if current.revision != expected_revision:
            raise StaleProgramRevision(
                f"expected revision {expected_revision}, current revision {current.revision}"
            )
        if current.status is not ProgramStatus.CREATED:
            raise InvalidStateTransition(
                f"{current.status.value} -> {ProgramStatus.ACTIVE.value}"
            )
        program = self._programs.transition(
            program_id,
            ProgramStatus.ACTIVE,
            expected_revision=expected_revision,
        )
        return self._view(program)

    def pause(
        self,
        program_id: str,
        *,
        expected_revision: int,
        expected_control_revision: int,
    ) -> dict[str, Any]:
        self._ensure_open()
        program_id = self._require_text(program_id, field="program_id")
        self._controls.pause(
            program_id,
            expected_program_revision=expected_revision,
            expected_control_revision=expected_control_revision,
        )
        return self._view(self._programs.get(program_id))

    def resume(
        self,
        program_id: str,
        *,
        expected_revision: int,
        expected_control_revision: int,
    ) -> dict[str, Any]:
        self._ensure_open()
        program_id = self._require_text(program_id, field="program_id")
        self._controls.resume(
            program_id,
            expected_program_revision=expected_revision,
            expected_control_revision=expected_control_revision,
        )
        return self._view(self._programs.get(program_id))

    def cancel(self, program_id: str, *, expected_revision: int) -> dict[str, Any]:
        self._ensure_open()
        program_id = self._require_text(program_id, field="program_id")
        self._controls.verify_integrity(program_id)
        program = self._programs.transition(
            program_id,
            ProgramStatus.CANCELLED,
            expected_revision=expected_revision,
        )
        return self._view(program)
