from __future__ import annotations

from pathlib import Path
from typing import Any

from ..kernel.durable_program import ProgramRepository
from ..kernel.enums import ProgramStatus
from ..kernel.errors import InvalidRequest
from ..kernel.models import Program
from ..kernel.serialization import to_canonical_data


class LocalProgramOperator:
    """Local product adapter over Host-owned durable Program authority."""

    def __init__(self, programs: ProgramRepository, *, owns_repository: bool = False):
        self._programs = programs
        self._owns_repository = owns_repository
        self._closed = False

    @classmethod
    def open(cls, database_path: str | Path) -> "LocalProgramOperator":
        return cls(ProgramRepository(database_path), owns_repository=True)

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

    @staticmethod
    def _require_text(value: str, *, field: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise InvalidRequest(f"{field} must be non-empty")
        return value

    def _view(self, program: Program) -> dict[str, Any]:
        self._programs.verify_integrity(program.program_id)
        events = self._programs.list_events(program.program_id)
        return {
            "program": to_canonical_data(program),
            "event_count": len(events),
        }

    def create(
        self,
        *,
        program_id: str,
        objective: str,
        constraints: tuple[str, ...] = (),
        success_criteria: tuple[str, ...] = (),
    ) -> dict[str, Any]:
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
        return tuple(self._view(program) for program in self._programs.list_programs())

    def show(self, program_id: str) -> dict[str, Any]:
        program_id = self._require_text(program_id, field="program_id")
        return self._view(self._programs.get(program_id))

    def start(self, program_id: str, *, expected_revision: int) -> dict[str, Any]:
        program_id = self._require_text(program_id, field="program_id")
        program = self._programs.transition(
            program_id,
            ProgramStatus.ACTIVE,
            expected_revision=expected_revision,
        )
        return self._view(program)

    def cancel(self, program_id: str, *, expected_revision: int) -> dict[str, Any]:
        program_id = self._require_text(program_id, field="program_id")
        program = self._programs.transition(
            program_id,
            ProgramStatus.CANCELLED,
            expected_revision=expected_revision,
        )
        return self._view(program)
