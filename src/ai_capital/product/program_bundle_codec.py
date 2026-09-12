from __future__ import annotations

import base64
import json
from typing import Any

from ..kernel.durable_program import ProgramRepository
from ..kernel.enums import ProgramStatus
from ..kernel.errors import InvalidRequest
from ..kernel.events import verify_event_digest
from ..kernel.models import Event, Program
from ..kernel.program_control import ProgramControl
from ..kernel.schema_codec import record_from_json
from ..kernel.serialization import canonical_digest, canonical_json, to_canonical_data
from .program_bundle_audit import validate_bundle_audit
from .workspace_types import (
    BUNDLE_PREFIX,
    WorkspaceArtifact,
    WorkspaceSnapshot,
    manifest_digest,
    require_text,
    sha256_bytes,
    validate_artifact,
    validate_snapshot,
    validate_timestamp,
)


def strict_json_object(content: bytes) -> dict[str, Any]:
    if type(content) is not bytes or not content:
        raise InvalidRequest("Program bundle must contain canonical UTF-8 JSON bytes")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidRequest("Program bundle must be UTF-8") from exc

    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise InvalidRequest(f"Program bundle contains duplicate key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(text, object_pairs_hook=no_duplicates)
    except json.JSONDecodeError as exc:
        raise InvalidRequest("Program bundle contains invalid JSON") from exc
    if type(value) is not dict:
        raise InvalidRequest("Program bundle root must be an object")
    try:
        canonical = canonical_json(value).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise InvalidRequest("Program bundle contains non-canonical values") from exc
    if canonical != content:
        raise InvalidRequest("Program bundle must use canonical JSON encoding")
    return value


def decode_record(cls: type, value: object, *, field: str):
    if type(value) is not dict:
        raise InvalidRequest(f"Program bundle {field} must be an object")
    try:
        record = record_from_json(cls, canonical_json(value))
    except (TypeError, ValueError) as exc:
        raise InvalidRequest(f"Program bundle {field} cannot be decoded") from exc
    if not isinstance(record, cls):
        raise InvalidRequest(f"Program bundle {field} decoded wrong type")
    return record


def _validate_event_history(
    events_data: object,
    *,
    source_program_id: str,
    program: Program,
) -> tuple[Event, ...]:
    if type(events_data) is not list or not events_data:
        raise InvalidRequest("Program bundle requires Program Event history")
    events: list[Event] = []
    previous_sequence = 0
    for event_data in events_data:
        event = decode_record(Event, event_data, field="event")
        if event.program_id != source_program_id:
            raise InvalidRequest("Program bundle Event Program mismatch")
        if event.sequence <= previous_sequence or not verify_event_digest(event):
            raise InvalidRequest("Program bundle Event history is invalid")
        previous_sequence = event.sequence
        events.append(event)
    if events[0].event_type != "program.created":
        raise InvalidRequest("Program bundle Event history must begin with creation")
    validator = ProgramRepository(":memory:")
    try:
        with validator._transaction():
            for event in events:
                validator._insert_event(event)
        rebuilt = validator.rebuild(source_program_id)
    finally:
        validator.close()
    if rebuilt != program:
        raise InvalidRequest("Program bundle Event history does not rebuild Program state")
    return tuple(events)


def _program_statuses(events: tuple[Event, ...]) -> dict[int, ProgramStatus]:
    statuses: dict[int, ProgramStatus] = {}
    for event in events:
        snapshot = event.payload.get("program")
        try:
            candidate = record_from_json(Program, canonical_json(snapshot))
        except (TypeError, ValueError) as exc:
            raise InvalidRequest(
                "Program bundle Event Program snapshot cannot be decoded"
            ) from exc
        if not isinstance(candidate, Program):
            raise InvalidRequest("Program bundle Event Program snapshot is invalid")
        statuses[candidate.revision] = candidate.status
    return statuses


def _validate_control(
    value: object,
    *,
    program: Program,
    events: tuple[Event, ...],
) -> ProgramControl:
    control = decode_record(ProgramControl, value, field="control")
    if control.program_id != program.program_id:
        raise InvalidRequest("Program bundle control Program mismatch")
    if type(control.revision) is not int or type(control.program_revision) is not int:
        raise InvalidRequest("Program bundle control revisions are malformed")
    if control.revision < 0 or control.program_revision < 0:
        raise InvalidRequest("Program bundle control revisions are invalid")
    if control.program_revision > program.revision or type(control.paused) is not bool:
        raise InvalidRequest("Program bundle control state is invalid")
    if control.revision == 0:
        if (
            control.program_revision != program.revision
            or control.paused
            or control.last_reason_code is not None
            or control.changed_at is not None
        ):
            raise InvalidRequest("Program bundle default control state is invalid")
    else:
        expected_paused = control.revision % 2 == 1
        expected_reason = "user_paused" if expected_paused else "user_resumed"
        if (
            control.paused is not expected_paused
            or control.last_reason_code != expected_reason
            or not control.changed_at
        ):
            raise InvalidRequest("Program bundle persisted control state is invalid")
        validate_timestamp(control.changed_at, field="control changed_at")
        if _program_statuses(events).get(control.program_revision) is not ProgramStatus.ACTIVE:
            raise InvalidRequest(
                "Program bundle control is not anchored to an active Program revision"
            )
    return control


def _validate_workspace(
    value: object,
    *,
    program: Program,
) -> tuple[WorkspaceSnapshot, tuple[tuple[WorkspaceArtifact, bytes], ...]]:
    if type(value) is not dict or set(value) != {"snapshot", "artifacts"}:
        raise InvalidRequest("Program bundle workspace section is invalid")
    snapshot = decode_record(
        WorkspaceSnapshot,
        value["snapshot"],
        field="workspace snapshot",
    )
    validate_snapshot(snapshot)
    if (
        snapshot.program_id != program.program_id
        or snapshot.program_revision != program.revision
    ):
        raise InvalidRequest("Program bundle workspace snapshot Program anchor mismatch")
    artifact_data = value["artifacts"]
    if type(artifact_data) is not list:
        raise InvalidRequest("Program bundle workspace artifacts must be an array")
    captured: list[tuple[WorkspaceArtifact, bytes]] = []
    for item in artifact_data:
        if type(item) is not dict or set(item) != {"entry", "content_base64"}:
            raise InvalidRequest("Program bundle workspace artifact is malformed")
        entry = decode_record(
            WorkspaceArtifact,
            item["entry"],
            field="workspace artifact",
        )
        validate_artifact(entry)
        encoded = item["content_base64"]
        if type(encoded) is not str:
            raise InvalidRequest("Program bundle artifact content must be base64 text")
        try:
            exact = base64.b64decode(encoded, validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise InvalidRequest("Program bundle artifact content is invalid base64") from exc
        if len(exact) != entry.byte_length or sha256_bytes(exact) != entry.digest:
            raise InvalidRequest("Program bundle artifact bytes do not match metadata")
        captured.append((entry, exact))
    paths = [item[0].path for item in captured]
    if paths != sorted(paths) or len(set(paths)) != len(paths):
        raise InvalidRequest("Program bundle workspace artifacts are not canonically ordered")
    entries = tuple(entry for entry, _ in captured)
    if len(entries) != snapshot.entry_count:
        raise InvalidRequest("Program bundle workspace artifact count mismatch")
    if manifest_digest(program.program_id, program.revision, entries) != snapshot.manifest_digest:
        raise InvalidRequest("Program bundle workspace manifest digest mismatch")
    return snapshot, tuple(captured)


def validate_bundle(
    content: bytes,
) -> tuple[
    dict[str, Any],
    Program,
    WorkspaceSnapshot,
    tuple[tuple[WorkspaceArtifact, bytes], ...],
]:
    envelope = strict_json_object(content)
    if set(envelope) != {"bundle_id", "schema_version", "payload"}:
        raise InvalidRequest("Program bundle envelope fields are invalid")
    if type(envelope["schema_version"]) is not int or envelope["schema_version"] != 1:
        raise InvalidRequest("unsupported Program bundle schema version")
    bundle_id = require_text(envelope["bundle_id"], field="bundle_id")
    payload = envelope["payload"]
    if type(payload) is not dict:
        raise InvalidRequest("Program bundle payload must be an object")
    if bundle_id != f"{BUNDLE_PREFIX}{canonical_digest(payload)}":
        raise InvalidRequest("Program bundle identity does not match its payload")
    if set(payload) != {
        "source_program_id", "program", "program_digest", "events",
        "control", "workspace", "audit",
    }:
        raise InvalidRequest("Program bundle payload fields are invalid")
    source_program_id = require_text(payload["source_program_id"], field="source_program_id")
    program = decode_record(Program, payload["program"], field="program")
    if program.program_id != source_program_id:
        raise InvalidRequest("Program bundle source identity mismatch")
    if payload["program_digest"] != canonical_digest(program):
        raise InvalidRequest("Program bundle Program digest mismatch")
    events = _validate_event_history(
        payload["events"],
        source_program_id=source_program_id,
        program=program,
    )
    _validate_control(payload["control"], program=program, events=events)
    snapshot, artifacts = _validate_workspace(payload["workspace"], program=program)
    validate_bundle_audit(payload["audit"], program=program)
    return envelope, program, snapshot, artifacts


def encode_bundle(payload: dict[str, Any]) -> bytes:
    bundle_id = f"{BUNDLE_PREFIX}{canonical_digest(payload)}"
    envelope = {"bundle_id": bundle_id, "schema_version": 1, "payload": payload}
    return canonical_json(envelope).encode("utf-8")


def embedded_artifact(
    entry: WorkspaceArtifact,
    content: bytes,
) -> dict[str, Any]:
    return {
        "entry": to_canonical_data(entry),
        "content_base64": base64.b64encode(content).decode("ascii"),
    }
