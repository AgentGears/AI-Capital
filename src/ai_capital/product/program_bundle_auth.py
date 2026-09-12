from __future__ import annotations

from typing import Any

from ..kernel.durable_program import ProgramRepository
from ..kernel.errors import IntegrityViolation, InvalidRequest
from ..kernel.events import verify_event_digest
from ..kernel.models import Event, Program
from ..kernel.schema_codec import record_from_json
from ..kernel.serialization import canonical_digest, canonical_json, to_canonical_data
from .workspace_types import require_text, validate_digest


_AUTH_KEYS = {"operation_id", "receipt_digests", "events"}
_EVENT_ANCHOR_KEYS = {
    "sequence",
    "event_id",
    "event_type",
    "occurred_at",
    "recorded_at",
    "actor_id",
    "program_id",
    "correlation_id",
    "digest",
}
_OPERATION_EVENT_TYPES = {
    "operation.requested",
    "operation.admitted",
    "operation.started",
    "operation.finished",
    "operation.interrupted",
    "operation.reconciled",
}
_TERMINAL_RECEIPT_EVENTS = {
    "operation.finished",
    "operation.interrupted",
    "operation.reconciled",
}


def _event_anchor(event: Event) -> dict[str, Any]:
    return {
        "sequence": event.sequence,
        "event_id": event.event_id,
        "event_type": event.event_type,
        "occurred_at": event.occurred_at,
        "recorded_at": event.recorded_at,
        "actor_id": event.actor_id,
        "program_id": event.program_id,
        "correlation_id": event.correlation_id,
        "digest": event.digest,
    }


def _decode_event(value: object, *, field: str) -> Event:
    if type(value) is not dict:
        raise InvalidRequest(f"Program bundle {field} must be an object")
    try:
        event = record_from_json(Event, canonical_json(value))
    except (TypeError, ValueError) as exc:
        raise InvalidRequest(f"Program bundle {field} cannot be decoded") from exc
    if not isinstance(event, Event) or not verify_event_digest(event):
        raise InvalidRequest(f"Program bundle {field} digest is invalid")
    return event


def _require_operation_view(value: object) -> dict[str, Any]:
    if type(value) is not dict:
        raise IntegrityViolation("Operation audit view is malformed")
    operation = value.get("operation")
    receipts = value.get("receipts")
    events = value.get("events")
    if type(operation) is not dict or type(receipts) not in {list, tuple} or type(events) not in {
        list,
        tuple,
    }:
        raise IntegrityViolation("Operation audit view is malformed")
    return value


def build_operation_audit_auth(
    programs: ProgramRepository,
    operation_views: object,
) -> tuple[dict[str, Any], ...]:
    """Bind exported Operation audit data to authenticated Host receipt/Event records."""
    if type(operation_views) not in {list, tuple}:
        raise IntegrityViolation("Operation audit collection is malformed")
    result: list[dict[str, Any]] = []
    for raw_view in operation_views:
        view = _require_operation_view(raw_view)
        operation = view["operation"]
        operation_id = require_text(operation.get("operation_id"), field="operation_id")
        receipts = tuple(view["receipts"])
        rows = programs._db.execute(
            """
            SELECT receipt_id, operation_id, receipt_json, receipt_digest
            FROM operation_receipts
            WHERE operation_id = ? ORDER BY sequence
            """,
            (operation_id,),
        ).fetchall()
        if len(rows) != len(receipts):
            raise IntegrityViolation("Operation portable receipt coverage mismatch")
        receipt_digests: list[str] = []
        for row, receipt in zip(rows, receipts):
            if type(receipt) is not dict:
                raise IntegrityViolation("Operation portable receipt is malformed")
            digest = row["receipt_digest"]
            try:
                validate_digest(digest, field="Operation receipt digest")
            except InvalidRequest as exc:
                raise IntegrityViolation("Operation receipt digest is malformed") from exc
            if (
                row["operation_id"] != operation_id
                or row["receipt_id"] != receipt.get("receipt_id")
                or row["receipt_json"] != canonical_json(receipt)
                or digest != canonical_digest(receipt)
            ):
                raise IntegrityViolation("Operation portable receipt diverges from Host record")
            receipt_digests.append(str(digest))

        event_records: list[dict[str, Any]] = []
        for anchor in view["events"]:
            if type(anchor) is not dict or set(anchor) != _EVENT_ANCHOR_KEYS:
                raise IntegrityViolation("Operation portable Event anchor is malformed")
            event_id = require_text(anchor["event_id"], field="operation Event id")
            row = programs._db.execute(
                """
                SELECT sequence, event_id, program_id, event_type, event_json, event_digest
                FROM events WHERE event_id = ?
                """,
                (event_id,),
            ).fetchone()
            if row is None:
                raise IntegrityViolation("Operation portable Event is missing")
            try:
                event = record_from_json(Event, row["event_json"])
            except (TypeError, ValueError) as exc:
                raise IntegrityViolation("Operation portable Event cannot be decoded") from exc
            if (
                not isinstance(event, Event)
                or event.sequence != int(row["sequence"])
                or event.event_id != row["event_id"]
                or event.program_id != row["program_id"]
                or event.event_type != row["event_type"]
                or event.digest != row["event_digest"]
                or not verify_event_digest(event)
                or _event_anchor(event) != anchor
            ):
                raise IntegrityViolation("Operation portable Event row/digest binding mismatch")
            event_records.append(to_canonical_data(event))
        result.append(
            {
                "operation_id": operation_id,
                "receipt_digests": tuple(receipt_digests),
                "events": tuple(event_records),
            }
        )
    return tuple(result)


def validate_operation_audit_auth(
    value: object,
    *,
    program: Program,
    audit: object,
) -> None:
    """Authenticate complete portable Operation receipts and semantic Event provenance."""
    if type(value) is not list:
        raise InvalidRequest("Program bundle Operation audit authentication must be an array")
    if type(audit) is not dict or type(audit.get("operations")) is not list:
        raise InvalidRequest("Program bundle audit operations are malformed")
    operations = audit["operations"]
    if len(value) != len(program.operation_refs) or len(operations) != len(value):
        raise InvalidRequest("Program bundle Operation audit authentication coverage mismatch")

    for auth, view, expected_id in zip(value, operations, program.operation_refs):
        if type(auth) is not dict or set(auth) != _AUTH_KEYS:
            raise InvalidRequest("Program bundle Operation audit authentication shape is invalid")
        if type(view) is not dict:
            raise InvalidRequest("Program bundle Operation audit item is malformed")
        operation = view.get("operation")
        receipts = view.get("receipts")
        anchors = view.get("events")
        if type(operation) is not dict or type(receipts) is not list or type(anchors) is not list:
            raise InvalidRequest("Program bundle Operation audit item is malformed")
        operation_id = require_text(auth["operation_id"], field="Operation auth operation_id")
        if operation_id != expected_id or operation.get("operation_id") != expected_id:
            raise InvalidRequest("Program bundle Operation audit authentication identity mismatch")

        digests = auth["receipt_digests"]
        if type(digests) is not list or len(digests) != len(receipts):
            raise InvalidRequest("Program bundle Operation receipt digest coverage mismatch")
        for receipt, digest in zip(receipts, digests):
            if type(receipt) is not dict:
                raise InvalidRequest("Program bundle Operation receipt is malformed")
            try:
                validate_digest(digest, field="Operation receipt digest")
            except InvalidRequest as exc:
                raise InvalidRequest("Program bundle Operation receipt digest is invalid") from exc
            if canonical_digest(receipt) != digest:
                raise InvalidRequest("Program bundle Operation receipt digest mismatch")

        events = auth["events"]
        if type(events) is not list or len(events) != len(anchors):
            raise InvalidRequest("Program bundle Operation Event authentication coverage mismatch")
        receipt_cursor = 0
        for index, (raw_event, anchor) in enumerate(zip(events, anchors)):
            event = _decode_event(raw_event, field=f"Operation Event {index}")
            if event.event_type not in _OPERATION_EVENT_TYPES:
                raise InvalidRequest("Program bundle Operation Event type is invalid")
            if type(anchor) is not dict or anchor != _event_anchor(event):
                raise InvalidRequest("Program bundle Operation Event anchor/payload binding mismatch")
            payload = to_canonical_data(event.payload)
            if type(payload) is not dict or type(payload.get("operation")) is not dict:
                raise InvalidRequest("Program bundle Operation Event payload is malformed")
            snapshot = payload["operation"]
            if snapshot.get("operation_id") != operation_id:
                raise InvalidRequest("Program bundle Operation Event identity mismatch")

            if event.event_type == "operation.requested":
                if set(payload) != {"operation", "resolution"} or payload["resolution"] != view.get(
                    "resolution"
                ):
                    raise InvalidRequest("Program bundle requested Operation Event payload mismatch")
            elif event.event_type == "operation.admitted":
                if set(payload) != {"operation", "authority_receipt_ref"} or payload[
                    "authority_receipt_ref"
                ] != operation.get("authority_receipt_ref"):
                    raise InvalidRequest("Program bundle admitted Operation Event payload mismatch")
            else:
                if set(payload) != {"operation", "receipt"}:
                    raise InvalidRequest("Program bundle Operation Event payload shape is invalid")
                event_receipt = payload["receipt"]
                if event.event_type in _TERMINAL_RECEIPT_EVENTS:
                    if receipt_cursor >= len(receipts) or event_receipt != receipts[receipt_cursor]:
                        raise InvalidRequest("Program bundle Operation Event receipt binding mismatch")
                    receipt_cursor += 1
                elif event_receipt is not None:
                    raise InvalidRequest("Program bundle started Operation Event has a receipt")

            if index == len(events) - 1 and snapshot != operation:
                raise InvalidRequest("Program bundle Operation final Event/projection mismatch")
        if receipt_cursor != len(receipts):
            raise InvalidRequest("Program bundle Operation receipt/Event coverage mismatch")
