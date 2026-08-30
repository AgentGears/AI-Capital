from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from .models import Event, Program
from .serialization import canonical_digest, to_canonical_data


PROGRAM_EVENT_TYPES = frozenset({
    "program.created",
    "program.activated",
    "program.revised",
    "program.work_added",
    "program.work_satisfied",
    "program.blocked",
    "program.unblocked",
    "program.completion_proposed",
    "program.completion_rejected",
    "program.completed",
    "program.failed",
    "program.cancelled",
})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def event_digest_fields(
    *,
    event_id: str,
    sequence: int,
    event_type: str,
    occurred_at: str,
    recorded_at: str,
    payload: object,
    actor_id: str | None,
    program_id: str | None,
    causation_id: str | None,
    correlation_id: str | None,
) -> str:
    return canonical_digest({
        "actor_id": actor_id,
        "causation_id": causation_id,
        "correlation_id": correlation_id,
        "event_id": event_id,
        "event_type": event_type,
        "occurred_at": occurred_at,
        "payload": payload,
        "program_id": program_id,
        "recorded_at": recorded_at,
        "sequence": sequence,
    })


def make_program_event(
    *,
    sequence: int,
    event_type: str,
    program: Program,
    event_id: str | None = None,
    occurred_at: str | None = None,
    recorded_at: str | None = None,
    actor_id: str | None = None,
    causation_id: str | None = None,
    correlation_id: str | None = None,
) -> Event:
    if event_type not in PROGRAM_EVENT_TYPES:
        raise ValueError(f"unknown Program event type: {event_type}")
    event_id = event_id or str(uuid4())
    occurred_at = occurred_at or utc_now()
    recorded_at = recorded_at or utc_now()
    payload = {"program": to_canonical_data(program)}
    digest = event_digest_fields(
        event_id=event_id,
        sequence=sequence,
        event_type=event_type,
        occurred_at=occurred_at,
        recorded_at=recorded_at,
        payload=payload,
        actor_id=actor_id,
        program_id=program.program_id,
        causation_id=causation_id,
        correlation_id=correlation_id,
    )
    return Event(
        event_id=event_id,
        sequence=sequence,
        event_type=event_type,
        occurred_at=occurred_at,
        recorded_at=recorded_at,
        payload=payload,
        digest=digest,
        actor_id=actor_id,
        program_id=program.program_id,
        causation_id=causation_id,
        correlation_id=correlation_id,
    )


def verify_event_digest(event: Event) -> bool:
    expected = event_digest_fields(
        event_id=event.event_id,
        sequence=event.sequence,
        event_type=event.event_type,
        occurred_at=event.occurred_at,
        recorded_at=event.recorded_at,
        payload=event.payload,
        actor_id=event.actor_id,
        program_id=event.program_id,
        causation_id=event.causation_id,
        correlation_id=event.correlation_id,
    )
    return expected == event.digest
