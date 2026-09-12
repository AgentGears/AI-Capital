from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..kernel.actor_store import ActorRepository
from ..kernel.authority import AuthorityEngine, PolicySnapshot, grant_is_current
from ..kernel.authority_store import AuthorityRepository
from ..kernel.capability_broker import CapabilityBroker, CapabilityHandlerRegistry
from ..kernel.capability_store import CapabilityRepository, capability_descriptor
from ..kernel.durable_program import ProgramRepository
from ..kernel.enums import AuthorityDecisionKind, ProgramStatus
from ..kernel.errors import AuthorityDenied, InvalidRequest
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
        self._workspace_root.mkdir(parents=True, exist_ok=True)
        self._artifact_root.mkdir(parents=True, exist_ok=True)
        self._actors = ActorRepository(programs)
        self._capabilities = CapabilityRepository(programs)
        self._handlers = CapabilityHandlerRegistry()
        install_product_capabilities(self._capabilities, self._handlers)
        self._broker = CapabilityBroker(self._capabilities, self._handlers)
        self._authority_store = AuthorityRepository(programs)
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
        base = {
            "decision": to_canonical_data(context.decision),
            "resolution": to_canonical_data(resolution),
        }
        if context.decision.decision is AuthorityDecisionKind.DENY:
            return {**base, "state": "denied", "operation": None}
        if context.decision.decision is AuthorityDecisionKind.ASK:
            return {**base, "state": "approval_required", "operation": None}
        authority_receipt = self._authority.issue_execution_authority(
            decision_id=context.decision.decision_id,
        )
        return self._execute(
            resolution=resolution,
            authority_receipt_id=authority_receipt.receipt_id,
            decision=base["decision"],
        )

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
            resolution=context.resolution,
            authority_receipt_id=authority_receipt.receipt_id,
            decision=to_canonical_data(context.decision),
        )

    def _execute(
        self,
        *,
        resolution,
        authority_receipt_id: str,
        decision: object,
    ) -> dict[str, Any]:
        executor = ProductCapabilityExecutor(
            resolution.capability_id,
            workspace_root=self._workspace_root,
            artifact_root=self._artifact_root,
        )
        operation = self._host.execute_authorized(
            resolution=resolution,
            authority_receipt_id=authority_receipt_id,
            executor=executor,
        )
        program = self._link_operation(operation)
        receipt = self._journal.execution_receipt(operation.operation_id)
        return {
            "state": "executed",
            "decision": decision,
            "resolution": to_canonical_data(resolution),
            "operation": to_canonical_data(operation),
            "execution_receipt": to_canonical_data(receipt),
            "program_revision": program.revision,
        }

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
