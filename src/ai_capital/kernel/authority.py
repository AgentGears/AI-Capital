from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from .actor_store import ActorRepository
from .capability_store import CapabilityRepository
from .durable_program import ProgramRepository
from .enums import ActorStatus, AuthorityDecisionKind, EffectClass, ProgramStatus, RiskClass
from .errors import (
    ApprovalConsumed,
    ApprovalInvalid,
    ApprovalRequired,
    AuthorityDenied,
    IntegrityViolation,
    PersistenceConflict,
    StaleActorGeneration,
    StaleCapabilityBinding,
    StaleProgramRevision,
)
from .events import utc_now
from .models import (
    AuthorityDecision,
    CapabilityResolution,
    ExecutionAuthorityReceipt,
    Grant,
)
from .schema_codec import record_from_json, record_to_json
from .serialization import canonical_digest, canonical_json


@dataclass(frozen=True, slots=True)
class PolicySnapshot:
    policy_revision: int
    ask_risk_classes: tuple[RiskClass, ...]
    deny_effect_classes: tuple[EffectClass, ...]
    created_at: str


@dataclass(frozen=True, slots=True)
class AuthorityDecisionContext:
    decision: AuthorityDecision
    program_id: str
    program_revision: int
    actor_id: str
    actor_generation: int
    capability_id: str
    capability_binding_revision: int
    resolution: CapabilityResolution


@dataclass(frozen=True, slots=True)
class ApprovalReceipt:
    approval_id: str
    decision_id: str
    resolved_effect_digest: str
    policy_revision: int
    issued_at: str
    single_use_identity: str


_EFFECT_RANK: dict[EffectClass, int] = {
    EffectClass.OBSERVE: 0,
    EffectClass.CREATE: 1,
    EffectClass.MODIFY: 2,
    EffectClass.DELETE: 3,
    EffectClass.EXTERNAL_SIDE_EFFECT: 4,
}


def effect_allowed_by_ceiling(ceiling: EffectClass, effect: EffectClass) -> bool:
    return _EFFECT_RANK[effect] <= _EFFECT_RANK[ceiling]


def scope_matches(pattern: str, value: str) -> bool:
    if pattern == "*":
        return True
    if "*" not in pattern:
        return pattern == value
    if pattern.count("*") != 1 or not pattern.endswith("*"):
        return False
    return value.startswith(pattern[:-1])


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("authority timestamps must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def grant_is_current(grant: Grant, *, at: str) -> bool:
    current = _parse_time(at)
    if current < _parse_time(grant.issued_at):
        return False
    if grant.expires_at is None:
        return True
    return current < _parse_time(grant.expires_at)


def grant_matches(
    grant: Grant,
    *,
    actor_id: str,
    resolution: CapabilityResolution,
    at: str,
) -> bool:
    if grant.subject_ref not in {f"actor:{actor_id}", "*"}:
        return False
    if not grant_is_current(grant, at=at):
        return False
    if not any(scope_matches(pattern, resolution.capability_id) for pattern in grant.capability_scope):
        return False
    if not any(scope_matches(pattern, resolution.resolved_effect.target) for pattern in grant.resource_scope):
        return False
    if not effect_allowed_by_ceiling(
        grant.effect_ceiling,
        resolution.resolved_effect.effect_class,
    ):
        return False
    if any(constraint != "approval_required" for constraint in grant.constraints):
        return False
    return True


class AuthorityEngine:
    """K4 Host authority boundary. It decides and receipts; it never executes effects."""

    def __init__(
        self,
        programs: ProgramRepository,
        actors: ActorRepository,
        capabilities: CapabilityRepository,
        authority_store,
    ):
        self._programs = programs
        self._actors = actors
        self._capabilities = capabilities
        self._store = authority_store

    @staticmethod
    def _require_active_context(*, program, actor) -> None:
        if program.status is not ProgramStatus.ACTIVE:
            raise AuthorityDenied("protected authority requires an active Program")
        if actor.status is not ActorStatus.ACTIVE:
            raise AuthorityDenied("protected authority requires an active Actor")

    @staticmethod
    def _expected_decision(
        *,
        capability,
        policy: PolicySnapshot,
        grants: tuple[Grant, ...],
    ) -> tuple[AuthorityDecisionKind, str]:
        if capability.effect_class in policy.deny_effect_classes:
            return AuthorityDecisionKind.DENY, "policy_denied_effect"
        if not grants:
            return AuthorityDecisionKind.DENY, "no_applicable_grant"
        grant_forces_approval = all(
            "approval_required" in grant.constraints for grant in grants
        )
        if capability.risk_class in policy.ask_risk_classes or grant_forces_approval:
            return AuthorityDecisionKind.ASK, "approval_required"
        return AuthorityDecisionKind.ALLOW, "grant_and_policy_allow"

    def _validate_decision_semantics(
        self,
        *,
        context: AuthorityDecisionContext,
        capability,
        policy: PolicySnapshot,
        current_grants: dict[str, Grant],
        at: str,
    ) -> tuple[Grant, ...]:
        decision = context.decision
        resolution = context.resolution
        if decision.request_id != resolution.request_id:
            raise IntegrityViolation("AuthorityDecision request differs from resolution")
        if context.capability_id != resolution.capability_id:
            raise IntegrityViolation("AuthorityDecision Capability differs from resolution")
        if canonical_json(resolution.resolved_effect) != decision.resolved_effect:
            raise IntegrityViolation("AuthorityDecision effect differs from resolution")
        if tuple(sorted(set(decision.grant_refs))) != tuple(decision.grant_refs):
            raise IntegrityViolation("AuthorityDecision Grant references are not canonical")

        referenced: list[Grant] = []
        for grant_id in decision.grant_refs:
            grant = current_grants.get(grant_id)
            if grant is None or not grant_matches(
                grant,
                actor_id=context.actor_id,
                resolution=resolution,
                at=at,
            ):
                raise AuthorityDenied("AuthorityDecision Grant set is no longer current")
            referenced.append(grant)

        expected_kind, expected_rationale = self._expected_decision(
            capability=capability,
            policy=policy,
            grants=tuple(referenced),
        )
        if decision.decision is not expected_kind or decision.rationale_code != expected_rationale:
            raise IntegrityViolation(
                "stored AuthorityDecision disagrees with deterministic policy semantics"
            )
        return tuple(referenced)

    def decide(
        self,
        *,
        program_id: str,
        actor_id: str,
        resolution: CapabilityResolution,
    ) -> AuthorityDecisionContext:
        program = self._programs.get(program_id)
        actor = self._actors.get(actor_id)
        capability = self._capabilities.get(resolution.capability_id)
        self._require_active_context(program=program, actor=actor)
        policy = self._store.current_policy()
        now = utc_now()

        if resolution.binding_revision != capability.binding_revision:
            raise StaleCapabilityBinding("Capability resolution is stale")
        if (
            resolution.resolved_effect.resource_type != capability.resource_type
            or resolution.resolved_effect.effect_class is not capability.effect_class
        ):
            raise IntegrityViolation(
                "Capability resolution violates the current Capability contract"
            )
        if not resolution.request_id.strip():
            raise IntegrityViolation("Capability resolution request identity is empty")

        matching = tuple(
            grant
            for grant in self._store.active_grants(actor_id=actor_id)
            if grant_matches(grant, actor_id=actor_id, resolution=resolution, at=now)
        )
        matching = tuple(sorted(matching, key=lambda grant: grant.grant_id))
        decision_kind, rationale = self._expected_decision(
            capability=capability,
            policy=policy,
            grants=matching,
        )
        grant_refs = () if decision_kind is AuthorityDecisionKind.DENY else tuple(
            grant.grant_id for grant in matching
        )

        decision = AuthorityDecision(
            decision_id=str(uuid4()),
            request_id=resolution.request_id,
            resolved_effect=canonical_json(resolution.resolved_effect),
            decision=decision_kind,
            rationale_code=rationale,
            policy_revision=policy.policy_revision,
            grant_refs=grant_refs,
            decided_at=now,
        )
        context = AuthorityDecisionContext(
            decision=decision,
            program_id=program.program_id,
            program_revision=program.revision,
            actor_id=actor.actor_id,
            actor_generation=actor.generation,
            capability_id=capability.capability_id,
            capability_binding_revision=capability.binding_revision,
            resolution=resolution,
        )
        self._store.record_decision(context)
        return context

    def approve(self, *, decision_id: str) -> ApprovalReceipt:
        context = self._store.get_decision(decision_id)
        if context.decision.decision is not AuthorityDecisionKind.ASK:
            raise ApprovalInvalid("only an ask decision may receive approval")
        policy = self._store.current_policy()
        if policy.policy_revision != context.decision.policy_revision:
            raise ApprovalInvalid("approval request is stale for current policy")
        existing = self._store._host_store._db.execute(
            "SELECT 1 FROM approval_receipts WHERE decision_id = ? LIMIT 1",
            (decision_id,),
        ).fetchone()
        if existing is not None:
            raise ApprovalInvalid("AuthorityDecision already has an approval receipt")
        approval = ApprovalReceipt(
            approval_id=str(uuid4()),
            decision_id=decision_id,
            resolved_effect_digest=canonical_digest(context.decision.resolved_effect),
            policy_revision=policy.policy_revision,
            issued_at=utc_now(),
            single_use_identity=str(uuid4()),
        )
        self._store.record_approval(approval)
        return approval

    @staticmethod
    def _receipt_from_json(payload: str) -> ExecutionAuthorityReceipt:
        try:
            receipt = record_from_json(ExecutionAuthorityReceipt, payload)
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("execution authority cannot be decoded") from exc
        if not isinstance(receipt, ExecutionAuthorityReceipt):
            raise IntegrityViolation("decoded execution authority has wrong type")
        return receipt

    def _persist_execution_authority(
        self,
        receipt: ExecutionAuthorityReceipt,
        *,
        approval: ApprovalReceipt | None,
    ) -> None:
        try:
            with self._store._host_store._transaction():
                prior_rows = self._store._host_store._db.execute(
                    "SELECT receipt_json, receipt_digest FROM execution_authority_receipts"
                ).fetchall()
                for row in prior_rows:
                    prior = self._receipt_from_json(row["receipt_json"])
                    if canonical_digest(prior) != row["receipt_digest"]:
                        raise IntegrityViolation("execution authority digest mismatch")
                    if prior.decision_id == receipt.decision_id:
                        raise AuthorityDenied(
                            "AuthorityDecision already issued execution authority"
                        )

                if approval is not None:
                    row = self._store._host_store._db.execute(
                        "SELECT consumed_at FROM approval_receipts WHERE approval_id = ?",
                        (approval.approval_id,),
                    ).fetchone()
                    if row is None:
                        raise ApprovalInvalid(f"unknown approval: {approval.approval_id}")
                    if row["consumed_at"] is not None:
                        raise ApprovalConsumed(
                            f"approval already consumed: {approval.approval_id}"
                        )
                    cursor = self._store._host_store._db.execute(
                        """
                        UPDATE approval_receipts SET consumed_at = ?
                        WHERE approval_id = ? AND consumed_at IS NULL
                        """,
                        (utc_now(), approval.approval_id),
                    )
                    if cursor.rowcount != 1:
                        raise ApprovalConsumed(
                            f"approval already consumed: {approval.approval_id}"
                        )
                    self._store._append_event("approval.consumed", approval)

                self._store._host_store._db.execute(
                    """
                    INSERT INTO execution_authority_receipts(
                        receipt_id, single_use_identity, receipt_json,
                        receipt_digest, consumed_at
                    ) VALUES (?, ?, ?, ?, NULL)
                    """,
                    (
                        receipt.receipt_id,
                        receipt.single_use_identity,
                        record_to_json(receipt),
                        canonical_digest(receipt),
                    ),
                )
                self._store._append_event("authority.execution_issued", receipt)
        except sqlite3.IntegrityError as exc:
            raise PersistenceConflict(
                f"execution authority identity already exists: {receipt.receipt_id}"
            ) from exc

    def issue_execution_authority(
        self,
        *,
        decision_id: str,
        approval_id: str | None = None,
    ) -> ExecutionAuthorityReceipt:
        context = self._store.get_decision(decision_id)
        decision = context.decision
        program = self._programs.get(context.program_id)
        actor = self._actors.get(context.actor_id)
        capability = self._capabilities.get(context.capability_id)
        policy = self._store.current_policy()

        if program.revision != context.program_revision:
            raise StaleProgramRevision("AuthorityDecision is stale for Program")
        if actor.generation != context.actor_generation:
            raise StaleActorGeneration("AuthorityDecision is stale for Actor")
        if capability.binding_revision != context.capability_binding_revision:
            raise StaleCapabilityBinding("AuthorityDecision is stale for Capability")
        self._require_active_context(program=program, actor=actor)
        if policy.policy_revision != decision.policy_revision:
            raise IntegrityViolation("AuthorityDecision is stale for policy")
        if context.resolution.binding_revision != capability.binding_revision:
            raise StaleCapabilityBinding("AuthorityDecision resolution is stale")

        current_grants = {
            grant.grant_id: grant
            for grant in self._store.active_grants(actor_id=context.actor_id)
        }
        now = utc_now()
        self._validate_decision_semantics(
            context=context,
            capability=capability,
            policy=policy,
            current_grants=current_grants,
            at=now,
        )

        approval: ApprovalReceipt | None = None
        if decision.decision is AuthorityDecisionKind.DENY:
            raise AuthorityDenied(decision.rationale_code)
        if decision.decision is AuthorityDecisionKind.ASK:
            if approval_id is None:
                raise ApprovalRequired("AuthorityDecision requires one-shot approval")
            approval = self._store.get_approval(approval_id)
            if approval.decision_id != decision_id:
                raise ApprovalInvalid("approval belongs to a different decision")
            if approval.policy_revision != policy.policy_revision:
                raise ApprovalInvalid("approval is stale for policy")
            if approval.resolved_effect_digest != canonical_digest(decision.resolved_effect):
                raise ApprovalInvalid("approval is bound to a different effect")
        elif approval_id is not None:
            raise ApprovalInvalid("allow decisions do not consume approvals")

        receipt = ExecutionAuthorityReceipt(
            receipt_id=str(uuid4()),
            decision_id=decision.decision_id,
            program_id=context.program_id,
            program_revision=context.program_revision,
            actor_id=context.actor_id,
            actor_generation=context.actor_generation,
            capability_id=context.capability_id,
            capability_binding_revision=context.capability_binding_revision,
            policy_revision=policy.policy_revision,
            grant_refs=decision.grant_refs,
            resolved_effect_digest=canonical_digest(decision.resolved_effect),
            issued_at=utc_now(),
            single_use_identity=str(uuid4()),
        )
        self._persist_execution_authority(receipt, approval=approval)
        return receipt

    def _validate_consumed_approval(self, context: AuthorityDecisionContext) -> None:
        if context.decision.decision is not AuthorityDecisionKind.ASK:
            return
        row = self._store._host_store._db.execute(
            """
            SELECT receipt_json, receipt_digest, consumed_at
            FROM approval_receipts WHERE decision_id = ?
            """,
            (context.decision.decision_id,),
        ).fetchone()
        if row is None or row["consumed_at"] is None:
            raise ApprovalInvalid("ask decision lacks a consumed approval")
        try:
            approval = record_from_json(ApprovalReceipt, row["receipt_json"])
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("approval cannot be decoded") from exc
        if canonical_digest(approval) != row["receipt_digest"]:
            raise IntegrityViolation("approval digest mismatch")
        if approval.decision_id != context.decision.decision_id:
            raise IntegrityViolation("approval decision binding is invalid")
        if approval.policy_revision != context.decision.policy_revision:
            raise IntegrityViolation("approval policy binding is invalid")
        if approval.resolved_effect_digest != canonical_digest(context.decision.resolved_effect):
            raise IntegrityViolation("approval effect binding is invalid")

    def consume_execution_authority(
        self,
        *,
        receipt_id: str,
    ) -> ExecutionAuthorityReceipt:
        receipt = self._store.get_execution_authority(receipt_id)
        context = self._store.get_decision(receipt.decision_id)
        program = self._programs.get(receipt.program_id)
        actor = self._actors.get(receipt.actor_id)
        capability = self._capabilities.get(receipt.capability_id)
        policy = self._store.current_policy()

        if program.revision != receipt.program_revision:
            raise StaleProgramRevision("execution authority is stale for Program")
        if actor.generation != receipt.actor_generation:
            raise StaleActorGeneration("execution authority is stale for Actor")
        if capability.binding_revision != receipt.capability_binding_revision:
            raise StaleCapabilityBinding("execution authority is stale for Capability")
        self._require_active_context(program=program, actor=actor)
        if policy.policy_revision != receipt.policy_revision:
            raise IntegrityViolation("execution authority is stale for policy")
        if receipt.resolved_effect_digest != canonical_digest(context.decision.resolved_effect):
            raise IntegrityViolation("execution authority effect binding is invalid")
        if tuple(receipt.grant_refs) != tuple(context.decision.grant_refs):
            raise IntegrityViolation("execution authority Grant set differs from decision")

        current_grants = {
            grant.grant_id: grant
            for grant in self._store.active_grants(actor_id=receipt.actor_id)
        }
        self._validate_decision_semantics(
            context=context,
            capability=capability,
            policy=policy,
            current_grants=current_grants,
            at=utc_now(),
        )
        self._validate_consumed_approval(context)

        self._store.consume_execution_authority(receipt_id)
        return receipt
