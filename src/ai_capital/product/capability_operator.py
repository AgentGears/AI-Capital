from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..kernel.actor_store import ActorRepository
from ..kernel.authority import AuthorityEngine, PolicySnapshot, grant_is_current
from ..kernel.authority_store import AuthorityRepository
from ..kernel.capability_broker import CapabilityBroker, CapabilityHandlerRegistry
from ..kernel.capability_store import CapabilityRepository, capability_descriptor
from ..kernel.durable_program import ProgramRepository
from ..kernel.enums import AuthorityDecisionKind, ExecutionOutcome, ProgramStatus
from ..kernel.errors import AuthorityDenied, IntegrityViolation, InvalidRequest
from ..kernel.events import utc_now
from ..kernel.models import CapabilityRequest, Grant, Operation
from ..kernel.operation_journal import OperationHost, OperationJournal
from ..kernel.program_control import ProgramControlRepository
from ..kernel.serialization import to_canonical_data
from .capability_catalog import (
    PRODUCT_CAPABILITY_IDS,
    capability_family,
    install_product_capabilities,
)
from .capability_executors import ProductCapabilityExecutor
from .reliability import ProductRequestRecord, ProductRequestRepository


_ROOT_BINDING_ID = "local-product-capability-roots-v1"
_ROOTED_AUTHORITY_TABLES = (
    "grants",
    "authority_decisions",
    "approval_receipts",
    "execution_authority_receipts",
)


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _authority_store_paths(programs: ProgramRepository) -> tuple[Path, ...]:
    database_path = str(programs._database_path)
    if database_path == ":memory:":
        return ()
    raw = Path(database_path)
    parent = raw.parent.resolve()
    database_entry = parent / raw.name
    lock_entry = parent / f"{raw.name}.writer.lock"
    paths = (
        database_entry,
        database_entry.resolve(),
        lock_entry,
        lock_entry.resolve(),
    )
    return tuple(dict.fromkeys(paths))


def _root_identity(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def _ensure_root_binding_table(programs: ProgramRepository) -> None:
    programs._db.execute(
        """
        CREATE TABLE IF NOT EXISTS product_capability_root_bindings (
            binding_id TEXT PRIMARY KEY,
            workspace_root TEXT NOT NULL,
            artifact_root TEXT NOT NULL
        )
        """
    )


def _root_binding(programs: ProgramRepository) -> tuple[str, str] | None:
    _ensure_root_binding_table(programs)
    row = programs._db.execute(
        """
        SELECT workspace_root, artifact_root
        FROM product_capability_root_bindings
        WHERE binding_id = ?
        """,
        (_ROOT_BINDING_ID,),
    ).fetchone()
    if row is None:
        return None
    return str(row["workspace_root"]), str(row["artifact_root"])


def _rooted_authority_exists(programs: ProgramRepository) -> bool:
    for table in _ROOTED_AUTHORITY_TABLES:
        row = programs._db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
        if row is not None:
            return True
    return False


def _validate_root_binding(
    programs: ProgramRepository,
    *,
    workspace_root: Path,
    artifact_root: Path,
) -> bool:
    expected = (_root_identity(workspace_root), _root_identity(artifact_root))
    current = _root_binding(programs)
    if current is None:
        if _rooted_authority_exists(programs):
            raise InvalidRequest(
                "unbound authority store already contains durable rooted authority state"
            )
        return False
    if current != expected:
        raise InvalidRequest(
            "capability roots do not match the durable authority root binding"
        )
    return True


def _prepare_capability_root(path: Path) -> None:
    try:
        if path.exists():
            if not path.is_dir():
                raise InvalidRequest("capability root must be a directory")
            return
        path.mkdir(parents=True, exist_ok=True)
    except InvalidRequest:
        raise
    except OSError as exc:
        raise InvalidRequest("capability root cannot be created as a directory") from exc
    if not path.is_dir():
        raise InvalidRequest("capability root must be a directory")


def _persist_root_binding(
    programs: ProgramRepository,
    *,
    workspace_root: Path,
    artifact_root: Path,
) -> None:
    expected = (_root_identity(workspace_root), _root_identity(artifact_root))
    with programs._transaction():
        row = programs._db.execute(
            """
            SELECT workspace_root, artifact_root
            FROM product_capability_root_bindings
            WHERE binding_id = ?
            """,
            (_ROOT_BINDING_ID,),
        ).fetchone()
        if row is not None:
            current = (str(row["workspace_root"]), str(row["artifact_root"]))
            if current != expected:
                raise InvalidRequest(
                    "capability roots changed while durable root binding was established"
                )
            return
        programs._db.execute(
            """
            INSERT INTO product_capability_root_bindings(
                binding_id, workspace_root, artifact_root
            ) VALUES (?, ?, ?)
            """,
            (_ROOT_BINDING_ID, *expected),
        )


class LocalCapabilityOperator:
    """Governed local product path from typed Capability request to durable Operation."""

    def __init__(
        self,
        programs: ProgramRepository,
        *,
        workspace_root: str | Path | None = None,
        artifact_root: str | Path | None = None,
        owns_repository: bool = False,
    ):
        database_path = str(programs._database_path)
        if workspace_root is None or artifact_root is None:
            if database_path == ":memory:":
                raise InvalidRequest(
                    "in-memory capability operators require explicit workspace and artifact roots"
                )
            parent = Path(database_path).resolve().parent
            if workspace_root is None:
                workspace_root = parent / "workspace"
            if artifact_root is None:
                artifact_root = parent / "generated-artifacts"
        self._programs = programs
        self._workspace_root = Path(workspace_root).resolve()
        self._artifact_root = Path(artifact_root).resolve()
        if _paths_overlap(self._workspace_root, self._artifact_root):
            raise InvalidRequest("workspace and artifact roots must be disjoint")
        for root in (self._workspace_root, self._artifact_root):
            for store_path in _authority_store_paths(programs):
                if root == store_path or root in store_path.parents:
                    raise InvalidRequest("authority store paths must be outside capability roots")

        self._authority_store = AuthorityRepository(programs)
        binding_exists = _validate_root_binding(
            programs,
            workspace_root=self._workspace_root,
            artifact_root=self._artifact_root,
        )
        _prepare_capability_root(self._workspace_root)
        _prepare_capability_root(self._artifact_root)

        self._actors = ActorRepository(programs)
        self._capabilities = CapabilityRepository(programs)
        self._handlers = CapabilityHandlerRegistry()
        install_product_capabilities(self._capabilities, self._handlers)
        self._broker = CapabilityBroker(self._capabilities, self._handlers)
        self._ensure_product_policy()
        self._authority = AuthorityEngine(
            programs,
            self._actors,
            self._capabilities,
            self._authority_store,
        )
        self._controls = ProgramControlRepository(programs)
        self._journal = OperationJournal(programs)
        self._host = OperationHost(self._journal, self._authority)
        self._requests = ProductRequestRepository(programs)
        if not binding_exists:
            _persist_root_binding(
                programs,
                workspace_root=self._workspace_root,
                artifact_root=self._artifact_root,
            )
        self._owns_repository = owns_repository
        self._closed = False

    @classmethod
    def open(
        cls,
        database_path: str | Path,
        *,
        workspace_root: str | Path | None = None,
        artifact_root: str | Path | None = None,
    ) -> "LocalCapabilityOperator":
        programs = ProgramRepository(database_path)
        try:
            return cls(
                programs,
                workspace_root=workspace_root,
                artifact_root=artifact_root,
                owns_repository=True,
            )
        except Exception:
            programs.close()
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_repository:
            self._programs.close()

    def __enter__(self) -> "LocalCapabilityOperator":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise InvalidRequest("local capability operator is closed")

    @staticmethod
    def _require_text(value: str, *, field: str) -> str:
        if type(value) is not str or not value.strip():
            raise InvalidRequest(f"{field} must be non-empty")
        return value

    def _ensure_product_policy(self) -> None:
        row = self._programs._db.execute(
            "SELECT COUNT(*) FROM authority_policies"
        ).fetchone()
        if int(row[0]) == 0:
            self._authority_store.install_policy(
                PolicySnapshot(
                    policy_revision=0,
                    ask_risk_classes=(),
                    deny_effect_classes=(),
                    created_at=utc_now(),
                )
            )
        else:
            self._authority_store.current_policy()

    def capabilities(self) -> tuple[dict[str, Any], ...]:
        self._ensure_open()
        views: list[dict[str, Any]] = []
        for capability_id in PRODUCT_CAPABILITY_IDS:
            capability = self._capabilities.get(capability_id)
            views.append(
                {
                    "family": capability_family(capability_id),
                    "capability": to_canonical_data(capability_descriptor(capability)),
                }
            )
        return tuple(views)

    def grants(self, actor_id: str) -> tuple[dict[str, Any], ...]:
        self._ensure_open()
        actor_id = self._require_text(actor_id, field="actor_id")
        self._actors.get(actor_id)
        now = utc_now()
        return tuple(
            to_canonical_data(grant)
            for grant in self._authority_store.active_grants(actor_id=actor_id)
            if grant_is_current(grant, at=now)
        )

    def grant(
        self,
        *,
        actor_id: str,
        capability_id: str,
        resource_scope: tuple[str, ...],
        approval_required: bool = False,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_open()
        actor_id = self._require_text(actor_id, field="actor_id")
        capability_id = self._require_text(capability_id, field="capability_id")
        if capability_id not in PRODUCT_CAPABILITY_IDS:
            raise InvalidRequest("Capability is not in the H2 product profile")
        if not resource_scope:
            raise InvalidRequest("resource_scope must contain at least one entry")
        for item in resource_scope:
            self._require_text(item, field="resource_scope")
        self._actors.get(actor_id)
        capability = self._capabilities.get(capability_id)
        grant = Grant(
            grant_id=str(uuid4()),
            subject_ref=f"actor:{actor_id}",
            capability_scope=(capability_id,),
            resource_scope=resource_scope,
            effect_ceiling=capability.effect_class,
            constraints=("approval_required",) if approval_required else (),
            issued_at=utc_now(),
            expires_at=expires_at,
            revision=0,
        )
        return to_canonical_data(self._authority_store.issue_grant(grant))

    def revoke_grant(self, grant_id: str) -> dict[str, Any]:
        self._ensure_open()
        grant_id = self._require_text(grant_id, field="grant_id")
        grant = self._authority_store.get_grant(grant_id)
        self._authority_store.revoke_grant(grant_id)
        return {"grant": to_canonical_data(grant), "revoked": True}

    def _require_program_ready(self, program_id: str) -> None:
        program = self._programs.get(program_id)
        if program.status is not ProgramStatus.ACTIVE:
            raise AuthorityDenied("capability invocation requires an active Program")
        self._controls.verify_integrity(program_id)
        control = self._controls.get(program_id)
        if control.paused:
            raise AuthorityDenied("capability invocation is blocked while Program is paused")

    @staticmethod
    def _request_payload(
        *,
        program_id: str,
        actor_id: str,
        capability_id: str,
        arguments: dict[str, object],
    ) -> dict[str, Any]:
        return {
            "program_id": program_id,
            "actor_id": actor_id,
            "capability_id": capability_id,
            "arguments": arguments,
        }

    def _decision_for_request(self, request_id: str):
        matches = []
        rows = self._programs._db.execute(
            "SELECT decision_id FROM authority_decisions ORDER BY decision_id"
        ).fetchall()
        for row in rows:
            context = self._authority_store.get_decision(str(row["decision_id"]))
            if context.resolution.request_id == request_id:
                matches.append(context)
        if len(matches) > 1:
            raise IntegrityViolation("product request maps to multiple Authority decisions")
        return None if not matches else matches[0]

    def _operation_for_request(self, request_id: str) -> Operation | None:
        matches: list[Operation] = []
        rows = self._programs._db.execute(
            "SELECT operation_id FROM operation_projections ORDER BY operation_id"
        ).fetchall()
        for row in rows:
            operation_id = str(row["operation_id"])
            if self._journal.resolution(operation_id).request_id == request_id:
                matches.append(self._journal.get(operation_id))
        if len(matches) > 1:
            raise IntegrityViolation("product request maps to multiple Operations")
        return None if not matches else matches[0]

    def _result_for_operation(self, operation: Operation, decision: object) -> dict[str, Any]:
        program = self._link_operation(operation)
        receipt = self._journal.execution_receipt(operation.operation_id)
        resolution = self._journal.resolution(operation.operation_id)
        return {
            "state": "executed",
            "decision": decision,
            "resolution": to_canonical_data(resolution),
            "operation": to_canonical_data(operation),
            "execution_receipt": to_canonical_data(receipt),
            "program_revision": program.revision,
        }

    def _recover_pending_request(self, record: ProductRequestRecord) -> dict[str, Any]:
        request_id = record.request_id
        context = (
            self._authority_store.get_decision(record.decision_id)
            if record.decision_id is not None
            else self._decision_for_request(request_id)
        )
        if context is not None and record.decision_id is None:
            record = self._requests.bind_decision(
                request_id,
                context.decision.decision_id,
            )

        operation = self._operation_for_request(request_id)
        if operation is not None and operation.execution_outcome in {
            ExecutionOutcome.NOT_STARTED,
            ExecutionOutcome.RUNNING,
        }:
            self._journal.recover_interrupted()
            operation = self._journal.get(operation.operation_id)

        if operation is not None:
            if context is None:
                raise IntegrityViolation("Operation request lacks AuthorityDecision context")
            result = self._result_for_operation(
                operation,
                to_canonical_data(context.decision),
            )
            return self._requests.complete(request_id, result).result or result

        if context is not None:
            base = {
                "decision": to_canonical_data(context.decision),
                "resolution": to_canonical_data(context.resolution),
            }
            if context.decision.decision is AuthorityDecisionKind.DENY:
                result = {**base, "state": "denied", "operation": None}
            elif context.decision.decision is AuthorityDecisionKind.ASK:
                result = {**base, "state": "approval_required", "operation": None}
            else:
                result = {
                    **base,
                    "state": "interrupted",
                    "reason_code": "interrupted_before_operation_admission",
                    "operation": None,
                }
        else:
            result = {
                "state": "interrupted",
                "reason_code": "interrupted_before_authority_decision",
                "operation": None,
            }
        return self._requests.complete(request_id, result).result or result

    def _resolve(
        self,
        *,
        capability_id: str,
        arguments: dict[str, object],
        request_id: str | None,
    ):
        if capability_id not in PRODUCT_CAPABILITY_IDS:
            raise InvalidRequest("Capability is not in the H2 product profile")
        capability = self._capabilities.get(capability_id)
        snapshot = self._broker.snapshot((capability_id,))
        request = CapabilityRequest(
            request_id or str(uuid4()),
            capability_id,
            arguments,
            capability.binding_revision,
        )
        return self._broker.resolve(request, snapshot=snapshot)

    def invoke(
        self,
        *,
        program_id: str,
        actor_id: str,
        capability_id: str,
        arguments: dict[str, object],
        request_id: str | None = None,
    ) -> dict[str, Any]:
        self._ensure_open()
        program_id = self._require_text(program_id, field="program_id")
        actor_id = self._require_text(actor_id, field="actor_id")
        capability_id = self._require_text(capability_id, field="capability_id")
        if type(arguments) is not dict:
            raise InvalidRequest("arguments must be an object")
        if request_id is not None:
            request_id = self._require_text(request_id, field="request_id")
            payload = self._request_payload(
                program_id=program_id,
                actor_id=actor_id,
                capability_id=capability_id,
                arguments=arguments,
            )
            try:
                prior = self._requests.get(request_id)
            except InvalidRequest:
                prior = None
            record = self._requests.begin(request_id, payload)
            if prior is not None:
                if record.state == "completed":
                    assert record.result is not None
                    return record.result
                return self._recover_pending_request(record)

        self._require_program_ready(program_id)
        self._actors.get(actor_id)
        resolution = self._resolve(
            capability_id=capability_id,
            arguments=arguments,
            request_id=request_id,
        )
        context = self._authority.decide(
            program_id=program_id,
            actor_id=actor_id,
            resolution=resolution,
        )
        if request_id is not None:
            self._requests.bind_decision(request_id, context.decision.decision_id)
        base = {
            "decision": to_canonical_data(context.decision),
            "resolution": to_canonical_data(resolution),
        }
        if context.decision.decision is AuthorityDecisionKind.DENY:
            result = {**base, "state": "denied", "operation": None}
            if request_id is not None:
                self._requests.complete(request_id, result)
            return result
        if context.decision.decision is AuthorityDecisionKind.ASK:
            result = {**base, "state": "approval_required", "operation": None}
            if request_id is not None:
                self._requests.complete(request_id, result)
            return result
        authority_receipt = self._authority.issue_execution_authority(
            decision_id=context.decision.decision_id,
        )
        result = self._execute(
            program_id=program_id,
            resolution=resolution,
            authority_receipt_id=authority_receipt.receipt_id,
            decision=base["decision"],
        )
        if request_id is not None:
            self._requests.complete(request_id, result)
        return result

    def execute_approved(
        self,
        *,
        decision_id: str,
        approval_id: str,
    ) -> dict[str, Any]:
        self._ensure_open()
        decision_id = self._require_text(decision_id, field="decision_id")
        approval_id = self._require_text(approval_id, field="approval_id")
        context = self._authority_store.get_decision(decision_id)
        self._require_program_ready(context.program_id)
        authority_receipt = self._authority.issue_execution_authority(
            decision_id=decision_id,
            approval_id=approval_id,
        )
        return self._execute(
            program_id=context.program_id,
            resolution=context.resolution,
            authority_receipt_id=authority_receipt.receipt_id,
            decision=to_canonical_data(context.decision),
        )

    def _execute(
        self,
        *,
        program_id: str,
        resolution,
        authority_receipt_id: str,
        decision: object,
    ) -> dict[str, Any]:
        executor = ProductCapabilityExecutor(
            resolution.capability_id,
            workspace_root=self._workspace_root,
            artifact_root=self._artifact_root,
            before_dispatch=lambda: self._require_program_ready(program_id),
        )
        operation = self._host.execute_authorized(
            resolution=resolution,
            authority_receipt_id=authority_receipt_id,
            executor=executor,
        )
        return self._result_for_operation(operation, decision)

    def _link_operation(self, operation: Operation):
        current = self._programs.get(operation.program_id)
        if operation.operation_id in current.operation_refs:
            return current
        return self._programs._commit_change(
            program_id=current.program_id,
            expected_revision=current.revision,
            event_type="program.revised",
            mutate=lambda program: replace(
                program,
                revision=program.revision + 1,
                operation_refs=program.operation_refs + (operation.operation_id,),
            ),
            event_id=None,
            occurred_at=None,
            recorded_at=None,
        )
