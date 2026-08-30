from dataclasses import replace

from .enums import ProgramStatus, WorkItemStatus
from .errors import InvalidRequest, InvalidStateTransition, StaleProgramRevision
from .models import Program, WorkItem


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

_WORK_MUTABLE_STATUSES = frozenset({
    ProgramStatus.CREATED,
    ProgramStatus.ACTIVE,
    ProgramStatus.BLOCKED,
})


def _require_revision(program: Program, expected_revision: int) -> None:
    if program.revision != expected_revision:
        raise StaleProgramRevision(
            f"expected revision {expected_revision}, current revision {program.revision}"
        )


def may_transition(source: ProgramStatus, target: ProgramStatus) -> bool:
    return target in _ALLOWED_TRANSITIONS[source]


def transition_program(
    program: Program,
    target: ProgramStatus,
    *,
    expected_revision: int,
) -> Program:
    _require_revision(program, expected_revision)
    if not may_transition(program.status, target):
        raise InvalidStateTransition(f"{program.status.value} -> {target.value}")
    return replace(program, status=target, revision=program.revision + 1)


def add_work_item(
    program: Program,
    work_item: WorkItem,
    *,
    expected_revision: int,
) -> Program:
    _require_revision(program, expected_revision)
    if program.status not in _WORK_MUTABLE_STATUSES:
        raise InvalidStateTransition(
            f"cannot add work while Program is {program.status.value}"
        )
    if work_item.status is not WorkItemStatus.OPEN:
        raise InvalidRequest("new work item must begin open")
    if any(item.work_item_id == work_item.work_item_id for item in program.work_items):
        raise InvalidRequest(f"duplicate work item: {work_item.work_item_id}")
    return replace(
        program,
        work_items=program.work_items + (work_item,),
        revision=program.revision + 1,
    )


def satisfy_work_item(
    program: Program,
    work_item_id: str,
    *,
    expected_revision: int,
) -> Program:
    _require_revision(program, expected_revision)
    if program.status not in _WORK_MUTABLE_STATUSES:
        raise InvalidStateTransition(
            f"cannot satisfy work while Program is {program.status.value}"
        )

    found = False
    updated: list[WorkItem] = []
    for item in program.work_items:
        if item.work_item_id != work_item_id:
            updated.append(item)
            continue
        found = True
        if item.status is not WorkItemStatus.OPEN:
            raise InvalidStateTransition(
                f"work item {work_item_id} is already {item.status.value}"
            )
        updated.append(replace(item, status=WorkItemStatus.SATISFIED))

    if not found:
        raise InvalidRequest(f"unknown work item: {work_item_id}")

    return replace(
        program,
        work_items=tuple(updated),
        revision=program.revision + 1,
    )


def allowed_targets(status: ProgramStatus) -> frozenset[ProgramStatus]:
    return _ALLOWED_TRANSITIONS[status]
