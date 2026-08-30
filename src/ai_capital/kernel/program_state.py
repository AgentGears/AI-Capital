from dataclasses import replace

from .enums import ProgramStatus
from .errors import InvalidStateTransition, StaleProgramRevision
from .models import Program


_ALLOWED_TRANSITIONS: dict[ProgramStatus, frozenset[ProgramStatus]] = {
    ProgramStatus.CREATED: frozenset({ProgramStatus.ACTIVE, ProgramStatus.CANCELLED}),
    ProgramStatus.ACTIVE: frozenset({
        ProgramStatus.BLOCKED,
        ProgramStatus.COMPLETION_PENDING,
        ProgramStatus.FAILED,
        ProgramStatus.CANCELLED,
    }),
    ProgramStatus.BLOCKED: frozenset({
        ProgramStatus.ACTIVE,
        ProgramStatus.FAILED,
        ProgramStatus.CANCELLED,
    }),
    ProgramStatus.COMPLETION_PENDING: frozenset({
        ProgramStatus.ACTIVE,
        ProgramStatus.BLOCKED,
        ProgramStatus.COMPLETED,
        ProgramStatus.FAILED,
        ProgramStatus.CANCELLED,
    }),
    ProgramStatus.COMPLETED: frozenset(),
    ProgramStatus.FAILED: frozenset(),
    ProgramStatus.CANCELLED: frozenset(),
}


def may_transition(source: ProgramStatus, target: ProgramStatus) -> bool:
    return target in _ALLOWED_TRANSITIONS[source]


def transition_program(
    program: Program,
    target: ProgramStatus,
    *,
    expected_revision: int,
) -> Program:
    if program.revision != expected_revision:
        raise StaleProgramRevision(
            f"expected revision {expected_revision}, current revision {program.revision}"
        )
    if not may_transition(program.status, target):
        raise InvalidStateTransition(f"{program.status.value} -> {target.value}")
    return replace(program, status=target, revision=program.revision + 1)


def allowed_targets(status: ProgramStatus) -> frozenset[ProgramStatus]:
    return _ALLOWED_TRANSITIONS[status]
