from __future__ import annotations

from typing import Any

from ..kernel.authority import ApprovalReceipt, AuthorityDecisionContext
from ..kernel.enums import (
    AuthorityDecisionKind,
    EffectStatus,
    ExecutionOutcome,
    ReconciliationStatus,
)
from ..kernel.errors import IntegrityViolation, InvalidRequest
from ..kernel.events import event_digest_fields
from ..kernel.evidence_store import EvidenceAdmissionReceipt
from ..kernel.models import (
    AuthorityDecision,
    CapabilityResolution,
    Evidence,
    ExecutionAuthorityReceipt,
    Operation,
    Program,
    Verification,
)
from ..kernel.operation_journal import ExecutionReceipt, ReconciliationReceipt
from ..kernel.operations import validate_operation_semantics
from ..kernel.schema_codec import record_from_json
from ..kernel.serialization import canonical_digest, canonical_json, to_canonical_data
from ..kernel.verification import VerificationContract
from .workspace_types import require_text, validate_digest, validate_timestamp


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
_OPERATION_KEYS = {"operation", "resolution", "idempotency_key", "receipts", "events"}
_EVIDENCE_KEYS = {"evidence", "admission", "artifact", "event"}
_VERIFICATION_KEYS = {
    "verification",
    "contract",
    "rationale_code",
    "currentness",
    "stale_reason",
    "event",
}
_ASK_KEYS = {
    "decision",
    "program_id",
    "program_revision",
    "actor_id",
    "actor_generation",
    "capability_id",
    "capability_binding_revision",
    "resolution",
    "approval_state",
    "currentness",
    "stale_reason",
    "decision_event",
    "approval",
    "execution_authority",
}
_APPROVAL_VIEW_KEYS = {"receipt", "consumed_at", "issued_event", "consumed_event"}
_EXECUTION_AUTHORITY_VIEW_KEYS = {
    "receipt",
    "consumed_at",
    "issued_event",
    "consumed_event",
}
_EXECUTION_RECEIPT_KEYS = {
    "receipt_id",
    "operation_id",
    "execution_outcome",
    "effect_status",
    "output",
    "backend_receipt_ref",
    "error_code",
    "observed_at",
    "idempotency_key",
}
_RECONCILIATION_RECEIPT_KEYS = {
    "receipt_id",
    "operation_id",
    "effect_status",
    "rationale_code",
    "evidence_refs",
    "backend_receipt_ref",
    "reconciled_at",
}
_OPERATION_EVENT_TYPES = {
    "operation.requested",
    "operation.admitted",
    "operation.started",
    "operation.finished",
    "operation.interrupted",
    "operation.reconciled",
}


def _exact_dict(value: object, expected: set[str], *, field: str) -> dict[str, Any]:
    if type(value) is not dict or set(value) != expected:
        raise InvalidRequest(f"Program bundle {field} shape is invalid")
    return value


def _decode(cls: type, value: object, *, field: str):
    if type(value) is not dict:
        raise InvalidRequest(f"Program bundle {field} must be an object")
    try:
        record = record_from_json(cls, canonical_json(value))
    except (TypeError, ValueError) as exc:
        raise InvalidRequest(f"Program bundle {field} cannot be decoded") from exc
    if not isinstance(record, cls):
        raise InvalidRequest(f"Program bundle {field} decoded wrong type")
    return record


def _nonnegative_int(value: object, *, field: str) -> int:
    if type(value) is not int or value < 0:
        raise InvalidRequest(f"Program bundle {field} is invalid")
    return value


def _nullable_text(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    return require_text(value, field=field)


def _anchor(
    value: object,
    *,
    field: str,
    event_type: str,
    actor_id: str | None,
    program_id: str | None,
    correlation_id: str | None,
    payload: object | None = None,
) -> dict[str, Any]:
    anchor = _exact_dict(value, _EVENT_ANCHOR_KEYS, field=field)
    sequence = anchor["sequence"]
    if type(sequence) is not int or sequence <= 0:
        raise InvalidRequest(f"Program bundle {field} sequence is invalid")
    require_text(anchor["event_id"], field=f"{field} event_id")
    if anchor["event_type"] != event_type:
        raise InvalidRequest(f"Program bundle {field} Event type is invalid")
    validate_timestamp(anchor["occurred_at"], field=f"{field} occurred_at")
    validate_timestamp(anchor["recorded_at"], field=f"{field} recorded_at")
    try:
        validate_digest(anchor["digest"], field=f"{field} digest")
    except InvalidRequest as exc:
        raise InvalidRequest(f"Program bundle {field} Event digest is invalid") from exc
    if (
        anchor["actor_id"] != actor_id
        or anchor["program_id"] != program_id
        or anchor["correlation_id"] != correlation_id
    ):
        raise InvalidRequest(f"Program bundle {field} Event context is invalid")
    if payload is not None:
        expected = event_digest_fields(
            event_id=anchor["event_id"],
            sequence=sequence,
            event_type=event_type,
            occurred_at=anchor["occurred_at"],
            recorded_at=anchor["recorded_at"],
            payload=to_canonical_data(payload),
            actor_id=actor_id,
            program_id=program_id,
            causation_id=None,
            correlation_id=correlation_id,
        )
        if anchor["digest"] != expected:
            raise InvalidRequest(f"Program bundle {field} Event digest mismatch")
    return anchor


def _validate_currentness(view: dict[str, Any], *, field: str, structured: bool) -> None:
    currentness = view["currentness"]
    stale_reason = view["stale_reason"]
    if currentness == "current":
        if stale_reason is not None:
            raise InvalidRequest(f"Program bundle {field} currentness is inconsistent")
        return
    if currentness != "stale":
        raise InvalidRequest(f"Program bundle {field} currentness is invalid")
    if structured:
        if (
            type(stale_reason) is not dict
            or set(stale_reason) != {"code", "message"}
            or type(stale_reason["code"]) is not str
            or not stale_reason["code"].strip()
            or type(stale_reason["message"]) is not str
            or not stale_reason["message"].strip()
        ):
            raise InvalidRequest(f"Program bundle {field} stale reason is invalid")
    elif type(stale_reason) is not str or not stale_reason.strip():
        raise InvalidRequest(f"Program bundle {field} stale reason is invalid")


def _validate_ask(view: object, *, program: Program) -> str:
    item = _exact_dict(view, _ASK_KEYS, field="ASK audit item")
    decision = _decode(AuthorityDecision, item["decision"], field="ASK decision")
    resolution = _decode(CapabilityResolution, item["resolution"], field="ASK resolution")
    if decision.decision is not AuthorityDecisionKind.ASK:
        raise InvalidRequest("Program bundle ASK audit is not an ASK decision")
    if item["program_id"] != program.program_id:
        raise InvalidRequest("Program bundle ASK audit Program mismatch")
    program_revision = _nonnegative_int(
        item["program_revision"], field="ASK Program revision"
    )
    actor_generation = _nonnegative_int(
        item["actor_generation"], field="ASK Actor generation"
    )
    capability_revision = _nonnegative_int(
        item["capability_binding_revision"], field="ASK Capability revision"
    )
    actor_id = require_text(item["actor_id"], field="ASK actor_id")
    capability_id = require_text(item["capability_id"], field="ASK capability_id")
    if program_revision > program.revision:
        raise InvalidRequest("Program bundle ASK audit references a future Program revision")
    if (
        decision.request_id != resolution.request_id
        or decision.resolved_effect != canonical_json(resolution.resolved_effect)
        or capability_id != resolution.capability_id
        or capability_revision != resolution.binding_revision
    ):
        raise InvalidRequest("Program bundle ASK decision/resolution binding mismatch")
    validate_timestamp(decision.decided_at, field="ASK decided_at")
    context = AuthorityDecisionContext(
        decision=decision,
        program_id=program.program_id,
        program_revision=program_revision,
        actor_id=actor_id,
        actor_generation=actor_generation,
        capability_id=capability_id,
        capability_binding_revision=capability_revision,
        resolution=resolution,
    )
    decision_anchor = _anchor(
        item["decision_event"],
        field="ASK decision",
        event_type="authority.decided",
        actor_id=actor_id,
        program_id=None,
        correlation_id=None,
        payload=context,
    )
    _validate_currentness(item, field="ASK", structured=True)

    approval_state = item["approval_state"]
    if approval_state not in {"awaiting_approval", "approved", "consumed"}:
        raise InvalidRequest("Program bundle ASK approval state is invalid")
    approval = item["approval"]
    approval_consumed_sequence: int | None = None
    if approval is None:
        if approval_state != "awaiting_approval":
            raise InvalidRequest("Program bundle ASK approval state lacks receipt")
    else:
        approval_view = _exact_dict(
            approval, _APPROVAL_VIEW_KEYS, field="ASK approval"
        )
        receipt = _decode(
            ApprovalReceipt, approval_view["receipt"], field="ASK approval receipt"
        )
        if (
            receipt.decision_id != decision.decision_id
            or receipt.resolved_effect_digest != canonical_digest(decision.resolved_effect)
            or receipt.policy_revision != decision.policy_revision
        ):
            raise InvalidRequest("Program bundle ASK approval binding mismatch")
        require_text(receipt.single_use_identity, field="approval single_use_identity")
        validate_timestamp(receipt.issued_at, field="approval issued_at")
        issued = _anchor(
            approval_view["issued_event"],
            field="ASK approval issued",
            event_type="approval.issued",
            actor_id=None,
            program_id=None,
            correlation_id=None,
            payload=receipt,
        )
        if issued["sequence"] <= decision_anchor["sequence"]:
            raise InvalidRequest("Program bundle ASK approval ordering is invalid")
        consumed_at = approval_view["consumed_at"]
        consumed_event = approval_view["consumed_event"]
        if consumed_at is None:
            if approval_state != "approved" or consumed_event is not None:
                raise InvalidRequest("Program bundle ASK approval state is inconsistent")
        else:
            validate_timestamp(consumed_at, field="approval consumed_at")
            if approval_state != "consumed" or consumed_event is None:
                raise InvalidRequest("Program bundle ASK consumed approval is inconsistent")
            consumed = _anchor(
                consumed_event,
                field="ASK approval consumed",
                event_type="approval.consumed",
                actor_id=None,
                program_id=None,
                correlation_id=None,
                payload=receipt,
            )
            if consumed["sequence"] <= issued["sequence"]:
                raise InvalidRequest("Program bundle ASK approval consumption ordering is invalid")
            approval_consumed_sequence = consumed["sequence"]

    execution = item["execution_authority"]
    if execution is not None:
        execution_view = _exact_dict(
            execution,
            _EXECUTION_AUTHORITY_VIEW_KEYS,
            field="ASK execution authority",
        )
        receipt = _decode(
            ExecutionAuthorityReceipt,
            execution_view["receipt"],
            field="ASK execution authority receipt",
        )
        if (
            approval_state != "consumed"
            or approval_consumed_sequence is None
            or receipt.decision_id != decision.decision_id
            or receipt.program_id != program.program_id
            or receipt.program_revision != program_revision
            or receipt.actor_id != actor_id
            or receipt.actor_generation != actor_generation
            or receipt.capability_id != capability_id
            or receipt.capability_binding_revision != capability_revision
            or receipt.policy_revision != decision.policy_revision
            or tuple(receipt.grant_refs) != tuple(decision.grant_refs)
            or receipt.resolved_effect_digest != canonical_digest(decision.resolved_effect)
        ):
            raise InvalidRequest("Program bundle ASK execution authority binding mismatch")
        require_text(receipt.single_use_identity, field="execution authority single_use_identity")
        validate_timestamp(receipt.issued_at, field="execution authority issued_at")
        issued = _anchor(
            execution_view["issued_event"],
            field="ASK execution authority issued",
            event_type="authority.execution_issued",
            actor_id=None,
            program_id=None,
            correlation_id=None,
            payload=receipt,
        )
        if issued["sequence"] <= approval_consumed_sequence:
            raise InvalidRequest("Program bundle execution authority ordering is invalid")
        consumed_at = execution_view["consumed_at"]
        consumed_event = execution_view["consumed_event"]
        if consumed_at is None:
            if consumed_event is not None:
                raise InvalidRequest("Program bundle execution authority state is inconsistent")
        else:
            validate_timestamp(consumed_at, field="execution authority consumed_at")
            if consumed_event is None:
                raise InvalidRequest("Program bundle consumed execution authority lacks Event")
            consumed = _anchor(
                consumed_event,
                field="ASK execution authority consumed",
                event_type="authority.execution_consumed",
                actor_id=None,
                program_id=None,
                correlation_id=None,
                payload=receipt,
            )
            if consumed["sequence"] <= issued["sequence"]:
                raise InvalidRequest("Program bundle execution authority consumption ordering is invalid")
    return decision.decision_id


def _validate_operation(view: object, *, program: Program, expected_id: str) -> None:
    item = _exact_dict(view, _OPERATION_KEYS, field="Operation audit item")
    operation = _decode(Operation, item["operation"], field="Operation audit record")
    resolution = _decode(
        CapabilityResolution, item["resolution"], field="Operation audit resolution"
    )
    if (
        operation.operation_id != expected_id
        or operation.program_id != program.program_id
        or operation.capability_id != resolution.capability_id
        or operation.request_digest != canonical_digest(resolution)
    ):
        raise InvalidRequest("Program bundle Operation audit binding mismatch")
    try:
        validate_operation_semantics(operation)
    except (IntegrityViolation, InvalidRequest) as exc:
        raise InvalidRequest("Program bundle Operation state is invalid") from exc
    idempotency_key = item["idempotency_key"]
    if idempotency_key is not None:
        try:
            validate_digest(idempotency_key, field="Operation idempotency key")
        except InvalidRequest as exc:
            raise InvalidRequest("Program bundle Operation idempotency key is invalid") from exc

    receipts_data = item["receipts"]
    if type(receipts_data) is not list:
        raise InvalidRequest("Program bundle Operation receipts must be an array")
    receipts: list[ExecutionReceipt | ReconciliationReceipt] = []
    execution_receipts: list[ExecutionReceipt] = []
    reconciliation_receipts: list[ReconciliationReceipt] = []
    for data in receipts_data:
        if type(data) is not dict:
            raise InvalidRequest("Program bundle Operation receipt is invalid")
        keys = set(data)
        if keys == _EXECUTION_RECEIPT_KEYS:
            receipt = _decode(ExecutionReceipt, data, field="Operation execution receipt")
            if receipt.operation_id != operation.operation_id:
                raise InvalidRequest("Program bundle execution receipt Operation mismatch")
            validate_timestamp(receipt.observed_at, field="execution receipt observed_at")
            if receipt.idempotency_key != idempotency_key:
                raise InvalidRequest("Program bundle execution receipt idempotency mismatch")
            execution_receipts.append(receipt)
        elif keys == _RECONCILIATION_RECEIPT_KEYS:
            receipt = _decode(
                ReconciliationReceipt, data, field="Operation reconciliation receipt"
            )
            if receipt.operation_id != operation.operation_id:
                raise InvalidRequest("Program bundle reconciliation receipt Operation mismatch")
            validate_timestamp(receipt.reconciled_at, field="reconciliation receipt reconciled_at")
            require_text(receipt.rationale_code, field="reconciliation rationale_code")
            reconciliation_receipts.append(receipt)
        else:
            raise InvalidRequest("Program bundle Operation receipt shape is invalid")
        receipts.append(receipt)
    if tuple(receipt.receipt_id for receipt in receipts) != operation.receipt_refs:
        raise InvalidRequest("Program bundle Operation receipt history mismatch")
    if len(execution_receipts) > 1:
        raise InvalidRequest("Program bundle Operation has multiple execution receipts")
    if reconciliation_receipts and not execution_receipts:
        raise InvalidRequest("Program bundle Operation reconciliation lacks execution receipt")
    if execution_receipts:
        execution = execution_receipts[0]
        if (
            operation.execution_outcome is not execution.execution_outcome
            or operation.finished_at != execution.observed_at
        ):
            raise InvalidRequest("Program bundle Operation terminal receipt mismatch")
        if reconciliation_receipts:
            last = reconciliation_receipts[-1]
            expected_reconciliation = (
                ReconciliationStatus.UNRESOLVED
                if last.effect_status is EffectStatus.INDETERMINATE
                else ReconciliationStatus.RESOLVED
            )
            if (
                operation.effect_status is not last.effect_status
                or operation.reconciliation_status is not expected_reconciliation
            ):
                raise InvalidRequest("Program bundle Operation reconciliation state mismatch")
        else:
            expected_reconciliation = (
                ReconciliationStatus.PENDING
                if execution.effect_status is EffectStatus.INDETERMINATE
                else ReconciliationStatus.NOT_REQUIRED
            )
            if (
                operation.effect_status is not execution.effect_status
                or operation.reconciliation_status is not expected_reconciliation
            ):
                raise InvalidRequest("Program bundle Operation execution state mismatch")

    events_data = item["events"]
    if type(events_data) is not list or not events_data:
        raise InvalidRequest("Program bundle Operation audit requires Event provenance")
    anchors: list[dict[str, Any]] = []
    previous_sequence = 0
    types: list[str] = []
    for index, data in enumerate(events_data):
        if type(data) is not dict:
            raise InvalidRequest("Program bundle Operation Event anchor is invalid")
        event_type = data.get("event_type")
        if event_type not in _OPERATION_EVENT_TYPES:
            raise InvalidRequest("Program bundle Operation Event type is invalid")
        anchor = _anchor(
            data,
            field=f"Operation Event {index}",
            event_type=event_type,
            actor_id=operation.actor_id,
            program_id=None,
            correlation_id=operation.program_id,
        )
        if anchor["sequence"] <= previous_sequence:
            raise InvalidRequest("Program bundle Operation Event ordering is invalid")
        previous_sequence = anchor["sequence"]
        anchors.append(anchor)
        types.append(event_type)
    if types[0] != "operation.requested" or types.count("operation.requested") != 1:
        raise InvalidRequest("Program bundle Operation Event history lacks unique request")
    if types.count("operation.admitted") > 1 or types.count("operation.started") > 1:
        raise InvalidRequest("Program bundle Operation Event history is invalid")
    if types.count("operation.finished") + types.count("operation.interrupted") > 1:
        raise InvalidRequest("Program bundle Operation has multiple terminal Events")
    if types.count("operation.reconciled") != len(reconciliation_receipts):
        raise InvalidRequest("Program bundle Operation reconciliation Event mismatch")
    if "operation.started" in types:
        if "operation.admitted" not in types or types.index("operation.admitted") > types.index("operation.started"):
            raise InvalidRequest("Program bundle Operation start ordering is invalid")
    terminal_positions = [
        types.index(kind)
        for kind in ("operation.finished", "operation.interrupted")
        if kind in types
    ]
    if reconciliation_receipts:
        if not terminal_positions or min(
            index for index, kind in enumerate(types) if kind == "operation.reconciled"
        ) < terminal_positions[0]:
            raise InvalidRequest("Program bundle Operation reconciliation ordering is invalid")
    if operation.execution_outcome is ExecutionOutcome.NOT_STARTED:
        if execution_receipts or any(
            kind in types for kind in {"operation.started", "operation.finished", "operation.interrupted", "operation.reconciled"}
        ):
            raise InvalidRequest("Program bundle not-started Operation audit is inconsistent")
    elif operation.execution_outcome is ExecutionOutcome.RUNNING:
        if execution_receipts or types[-1] != "operation.started":
            raise InvalidRequest("Program bundle running Operation audit is inconsistent")
    elif not execution_receipts:
        raise InvalidRequest("Program bundle terminal Operation lacks execution receipt")


def _validate_evidence(view: object, *, program: Program, expected_id: str) -> None:
    item = _exact_dict(view, _EVIDENCE_KEYS, field="Evidence audit item")
    evidence = _decode(Evidence, item["evidence"], field="Evidence audit record")
    admission = _decode(
        EvidenceAdmissionReceipt, item["admission"], field="Evidence admission receipt"
    )
    if evidence.evidence_id != expected_id:
        raise InvalidRequest("Program bundle Evidence identity mismatch")
    require_text(evidence.source_class, field="Evidence source_class")
    validate_timestamp(evidence.observed_at, field="Evidence observed_at")
    try:
        validate_digest(evidence.digest, field="Evidence digest")
    except InvalidRequest as exc:
        raise InvalidRequest("Program bundle Evidence digest is invalid") from exc
    if (
        type(evidence.provenance) is not tuple
        or not evidence.provenance
        or any(type(value) is not str or not value.strip() for value in evidence.provenance)
        or not evidence.trust_class.strip()
        or not evidence.currentness.strip()
    ):
        raise InvalidRequest("Program bundle Evidence metadata is invalid")
    if (
        admission.evidence_id != evidence.evidence_id
        or admission.artifact_digest != evidence.digest
    ):
        raise InvalidRequest("Program bundle Evidence admission binding mismatch")
    validate_timestamp(admission.admitted_at, field="Evidence admitted_at")
    artifact = item["artifact"]
    if type(artifact) is not dict or set(artifact) != {"content_ref", "digest", "byte_length"}:
        raise InvalidRequest("Program bundle Evidence artifact metadata is invalid")
    if (
        artifact["content_ref"] != evidence.content_ref
        or artifact["digest"] != evidence.digest
        or type(artifact["byte_length"]) is not int
        or artifact["byte_length"] <= 0
    ):
        raise InvalidRequest("Program bundle Evidence artifact binding mismatch")
    _anchor(
        item["event"],
        field="Evidence admitted",
        event_type="evidence.admitted",
        actor_id=None,
        program_id=None,
        correlation_id=evidence.evidence_id,
        payload={"evidence": evidence, "admission": admission},
    )


def _validate_verification(view: object, *, program: Program, expected_id: str) -> None:
    item = _exact_dict(view, _VERIFICATION_KEYS, field="Verification audit item")
    verification = _decode(
        Verification, item["verification"], field="Verification audit record"
    )
    contract = _decode(
        VerificationContract, item["contract"], field="Verification contract"
    )
    if (
        verification.verification_id != expected_id
        or verification.subject_ref != f"program:{program.program_id}"
        or verification.subject_revision > program.revision
        or verification.contract_ref != contract.contract_id
        or contract.program_id != program.program_id
    ):
        raise InvalidRequest("Program bundle Verification binding mismatch")
    try:
        validate_digest(verification.subject_digest, field="Verification subject digest")
    except InvalidRequest as exc:
        raise InvalidRequest("Program bundle Verification subject digest is invalid") from exc
    validate_timestamp(verification.performed_at, field="Verification performed_at")
    validate_timestamp(contract.created_at, field="Verification contract created_at")
    if (
        type(contract.mandatory) is not bool
        or type(contract.require_effect_certainty) is not bool
        or any(type(value) is not str or not value.strip() for value in contract.success_criteria)
        or any(type(value) is not str or not value.strip() for value in contract.required_claim_refs)
    ):
        raise InvalidRequest("Program bundle Verification contract is invalid")
    rationale = require_text(item["rationale_code"], field="Verification rationale_code")
    _validate_currentness(item, field="Verification", structured=False)
    _anchor(
        item["event"],
        field="Verification recorded",
        event_type="verification.recorded",
        actor_id=None,
        program_id=None,
        correlation_id=contract.program_id,
        payload={"verification": verification, "rationale_code": rationale},
    )


def validate_bundle_audit(value: object, *, program: Program) -> None:
    audit = _exact_dict(
        value,
        {"asks", "operations", "evidence", "verifications"},
        field="audit section",
    )
    if any(type(audit[key]) is not list for key in audit):
        raise InvalidRequest("Program bundle audit collections must be arrays")

    ask_ids: list[str] = []
    ask_sequences: list[int] = []
    for ask in audit["asks"]:
        ask_ids.append(_validate_ask(ask, program=program))
        ask_sequences.append(ask["decision_event"]["sequence"])
    if len(set(ask_ids)) != len(ask_ids) or ask_sequences != sorted(ask_sequences):
        raise InvalidRequest("Program bundle ASK audit ordering/identity is invalid")

    if len(audit["operations"]) != len(program.operation_refs):
        raise InvalidRequest("Program bundle operations audit coverage mismatch")
    for view, expected_id in zip(audit["operations"], program.operation_refs):
        _validate_operation(view, program=program, expected_id=expected_id)

    if len(audit["evidence"]) != len(program.evidence_refs):
        raise InvalidRequest("Program bundle evidence audit coverage mismatch")
    for view, expected_id in zip(audit["evidence"], program.evidence_refs):
        _validate_evidence(view, program=program, expected_id=expected_id)

    if len(audit["verifications"]) != len(program.verification_refs):
        raise InvalidRequest("Program bundle verifications audit coverage mismatch")
    for view, expected_id in zip(audit["verifications"], program.verification_refs):
        _validate_verification(view, program=program, expected_id=expected_id)
