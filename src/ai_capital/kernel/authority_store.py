from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from uuid import uuid4

from .authority import ApprovalReceipt, AuthorityDecisionContext, PolicySnapshot
from .durable_program import ProgramRepository
from .errors import (
    ApprovalConsumed,
    ApprovalInvalid,
    AuthorityDenied,
    IntegrityViolation,
    InvalidRequest,
    PersistenceConflict,
)
from .events import event_digest_fields, utc_now
from .models import Event, ExecutionAuthorityReceipt, Grant
from .schema_codec import record_from_json, record_to_json
from .serialization import canonical_digest, to_canonical_data


_COMPONENT = "authority"
_COMPONENT_SCHEMA_VERSION = 1


class AuthorityRepository:
    """Host-owned durable Grant, policy, decision, approval, and authority state."""

    def __init__(self, host_store: ProgramRepository):
        self._host_store = host_store
        self._migrate()

    def _migrate(self) -> None:
        with self._host_store._transaction():
            self._host_store._db.execute(
                """
                CREATE TABLE IF NOT EXISTS component_schema (
                    component TEXT PRIMARY KEY,
                    version INTEGER NOT NULL
                )
                """
            )
            row = self._host_store._db.execute(
                "SELECT version FROM component_schema WHERE component = ?",
                (_COMPONENT,),
            ).fetchone()
            if row is not None:
                version = int(row[0])
                if version > _COMPONENT_SCHEMA_VERSION:
                    raise IntegrityViolation(
                        f"Authority schema version {version} is newer than supported "
                        f"{_COMPONENT_SCHEMA_VERSION}"
                    )
                if version != _COMPONENT_SCHEMA_VERSION:
                    raise IntegrityViolation(f"unsupported Authority schema version {version}")
                return

            statements = (
                """
                CREATE TABLE IF NOT EXISTS grants (
                    grant_id TEXT PRIMARY KEY,
                    subject_ref TEXT NOT NULL,
                    grant_json TEXT NOT NULL,
                    grant_digest TEXT NOT NULL,
                    revoked_at TEXT
                )
                """,
                """
                CREATE INDEX IF NOT EXISTS grants_subject
                    ON grants(subject_ref, grant_id)
                """,
                """
                CREATE TABLE IF NOT EXISTS authority_policies (
                    policy_revision INTEGER PRIMARY KEY,
                    policy_json TEXT NOT NULL,
                    policy_digest TEXT NOT NULL
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS authority_decisions (
                    decision_id TEXT PRIMARY KEY,
                    context_json TEXT NOT NULL,
                    context_digest TEXT NOT NULL
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS approval_receipts (
                    approval_id TEXT PRIMARY KEY,
                    decision_id TEXT NOT NULL,
                    receipt_json TEXT NOT NULL,
                    receipt_digest TEXT NOT NULL,
                    consumed_at TEXT
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS execution_authority_receipts (
                    receipt_id TEXT PRIMARY KEY,
                    single_use_identity TEXT NOT NULL UNIQUE,
                    receipt_json TEXT NOT NULL,
                    receipt_digest TEXT NOT NULL,
                    consumed_at TEXT
                )
                """,
            )
            for statement in statements:
                self._host_store._db.execute(statement)
            self._host_store._db.execute(
                "INSERT INTO component_schema(component, version) VALUES (?, ?)",
                (_COMPONENT, _COMPONENT_SCHEMA_VERSION),
            )

    @staticmethod
    def _parse_time(value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise InvalidRequest("authority timestamp must be valid ISO-8601") from exc
        if parsed.tzinfo is None:
            raise InvalidRequest("authority timestamp must be timezone-aware")
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _validate_scope(pattern: str) -> None:
        if not pattern:
            raise InvalidRequest("Grant scope entries must be non-empty")
        if "*" in pattern and (pattern.count("*") != 1 or not pattern.endswith("*")):
            raise InvalidRequest("Grant wildcard must be a single trailing '*'")

    @classmethod
    def _validate_grant(cls, grant: Grant) -> None:
        if not grant.grant_id.strip() or not grant.subject_ref.strip():
            raise InvalidRequest("Grant identity and subject must be non-empty")
        if grant.revision != 0:
            raise InvalidRequest("new Grant must begin at revision 0")
        if not grant.capability_scope or not grant.resource_scope:
            raise InvalidRequest("Grant capability and resource scope must be non-empty")
        for pattern in grant.capability_scope + grant.resource_scope:
            cls._validate_scope(pattern)
        if any(item != "approval_required" for item in grant.constraints):
            raise InvalidRequest("unsupported Grant constraint")
        issued_at = cls._parse_time(grant.issued_at)
        if grant.expires_at is not None:
            expires_at = cls._parse_time(grant.expires_at)
            if expires_at <= issued_at:
                raise InvalidRequest("Grant expiry must be later than issuance")

    def _append_event(
        self,
        event_type: str,
        payload: object,
        *,
        actor_id: str | None = None,
    ) -> Event:
        sequence = self._host_store._next_sequence()
        event_id = str(uuid4())
        occurred_at = utc_now()
        recorded_at = utc_now()
        canonical_payload = to_canonical_data(payload)
        digest = event_digest_fields(
            event_id=event_id,
            sequence=sequence,
            event_type=event_type,
            occurred_at=occurred_at,
            recorded_at=recorded_at,
            payload=canonical_payload,
            actor_id=actor_id,
            program_id=None,
            causation_id=None,
            correlation_id=None,
        )
        event = Event(
            event_id=event_id,
            sequence=sequence,
            event_type=event_type,
            occurred_at=occurred_at,
            recorded_at=recorded_at,
            payload=canonical_payload,
            digest=digest,
            actor_id=actor_id,
            program_id=None,
        )
        self._host_store._db.execute(
            """
            INSERT INTO events(
                sequence, event_id, program_id, event_type, event_json, event_digest
            ) VALUES (?, ?, NULL, ?, ?, ?)
            """,
            (
                event.sequence,
                event.event_id,
                event.event_type,
                record_to_json(event),
                event.digest,
            ),
        )
        return event

    def issue_grant(self, grant: Grant) -> Grant:
        self._validate_grant(grant)
        try:
            with self._host_store._transaction():
                self._host_store._db.execute(
                    """
                    INSERT INTO grants(
                        grant_id, subject_ref, grant_json, grant_digest, revoked_at
                    ) VALUES (?, ?, ?, ?, NULL)
                    """,
                    (
                        grant.grant_id,
                        grant.subject_ref,
                        record_to_json(grant),
                        canonical_digest(grant),
                    ),
                )
                self._append_event("grant.issued", grant)
        except sqlite3.IntegrityError as exc:
            raise PersistenceConflict(f"Grant already exists: {grant.grant_id}") from exc
        return grant

    def _grant_from_row(self, row: sqlite3.Row) -> Grant:
        try:
            grant = record_from_json(Grant, row["grant_json"])
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("Grant cannot be decoded") from exc
        if not isinstance(grant, Grant):
            raise IntegrityViolation("decoded Grant has wrong type")
        if grant.grant_id != row["grant_id"] or grant.subject_ref != row["subject_ref"]:
            raise IntegrityViolation("Grant row identity mismatch")
        if canonical_digest(grant) != row["grant_digest"]:
            raise IntegrityViolation("Grant digest mismatch")
        return grant

    def get_grant(self, grant_id: str) -> Grant:
        row = self._host_store._db.execute(
            """
            SELECT grant_id, subject_ref, grant_json, grant_digest, revoked_at
            FROM grants WHERE grant_id = ?
            """,
            (grant_id,),
        ).fetchone()
        if row is None:
            raise InvalidRequest(f"unknown Grant: {grant_id}")
        return self._grant_from_row(row)

    def active_grants(self, *, actor_id: str) -> tuple[Grant, ...]:
        rows = self._host_store._db.execute(
            """
            SELECT grant_id, subject_ref, grant_json, grant_digest, revoked_at
            FROM grants
            WHERE revoked_at IS NULL AND subject_ref IN (?, '*')
            ORDER BY grant_id
            """,
            (f"actor:{actor_id}",),
        ).fetchall()
        return tuple(self._grant_from_row(row) for row in rows)

    def revoke_grant(self, grant_id: str) -> None:
        with self._host_store._transaction():
            grant = self.get_grant(grant_id)
            cursor = self._host_store._db.execute(
                "UPDATE grants SET revoked_at = ? WHERE grant_id = ? AND revoked_at IS NULL",
                (utc_now(), grant_id),
            )
            if cursor.rowcount != 1:
                raise AuthorityDenied(f"Grant is already revoked: {grant_id}")
            self._append_event("grant.revoked", {"grant_id": grant_id, "grant": grant})

    @classmethod
    def _validate_policy(cls, policy: PolicySnapshot) -> None:
        if policy.policy_revision < 0:
            raise InvalidRequest("policy revision must be non-negative")
        if len(set(policy.ask_risk_classes)) != len(policy.ask_risk_classes):
            raise InvalidRequest("policy ask risks contain duplicates")
        if len(set(policy.deny_effect_classes)) != len(policy.deny_effect_classes):
            raise InvalidRequest("policy denied effects contain duplicates")
        cls._parse_time(policy.created_at)

    def install_policy(self, policy: PolicySnapshot) -> PolicySnapshot:
        self._validate_policy(policy)
        row = self._host_store._db.execute(
            "SELECT MAX(policy_revision) FROM authority_policies"
        ).fetchone()
        current = row[0]
        expected = 0 if current is None else int(current) + 1
        if policy.policy_revision != expected:
            raise InvalidRequest(
                f"policy revision must be next contiguous revision {expected}"
            )
        with self._host_store._transaction():
            self._host_store._db.execute(
                """
                INSERT INTO authority_policies(
                    policy_revision, policy_json, policy_digest
                ) VALUES (?, ?, ?)
                """,
                (
                    policy.policy_revision,
                    record_to_json(policy),
                    canonical_digest(policy),
                ),
            )
            self._append_event("policy.installed", policy)
        return policy

    def current_policy(self) -> PolicySnapshot:
        row = self._host_store._db.execute(
            """
            SELECT policy_revision, policy_json, policy_digest
            FROM authority_policies ORDER BY policy_revision DESC LIMIT 1
            """
        ).fetchone()
        if row is None:
            raise IntegrityViolation("no Authority policy is installed")
        try:
            policy = record_from_json(PolicySnapshot, row["policy_json"])
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("Authority policy cannot be decoded") from exc
        if not isinstance(policy, PolicySnapshot):
            raise IntegrityViolation("decoded Authority policy has wrong type")
        if policy.policy_revision != int(row["policy_revision"]):
            raise IntegrityViolation("Authority policy row identity mismatch")
        if canonical_digest(policy) != row["policy_digest"]:
            raise IntegrityViolation("Authority policy digest mismatch")
        return policy

    def record_decision(self, context: AuthorityDecisionContext) -> None:
        try:
            with self._host_store._transaction():
                self._host_store._db.execute(
                    """
                    INSERT INTO authority_decisions(
                        decision_id, context_json, context_digest
                    ) VALUES (?, ?, ?)
                    """,
                    (
                        context.decision.decision_id,
                        record_to_json(context),
                        canonical_digest(context),
                    ),
                )
                self._append_event(
                    "authority.decided",
                    context,
                    actor_id=context.actor_id,
                )
        except sqlite3.IntegrityError as exc:
            raise PersistenceConflict(
                f"AuthorityDecision already exists: {context.decision.decision_id}"
            ) from exc

    def get_decision(self, decision_id: str) -> AuthorityDecisionContext:
        row = self._host_store._db.execute(
            """
            SELECT decision_id, context_json, context_digest
            FROM authority_decisions WHERE decision_id = ?
            """,
            (decision_id,),
        ).fetchone()
        if row is None:
            raise InvalidRequest(f"unknown AuthorityDecision: {decision_id}")
        try:
            context = record_from_json(AuthorityDecisionContext, row["context_json"])
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("AuthorityDecision context cannot be decoded") from exc
        if not isinstance(context, AuthorityDecisionContext):
            raise IntegrityViolation("decoded AuthorityDecision context has wrong type")
        if context.decision.decision_id != row["decision_id"]:
            raise IntegrityViolation("AuthorityDecision row identity mismatch")
        if canonical_digest(context) != row["context_digest"]:
            raise IntegrityViolation("AuthorityDecision context digest mismatch")
        return context

    def record_approval(self, approval: ApprovalReceipt) -> None:
        try:
            with self._host_store._transaction():
                self._host_store._db.execute(
                    """
                    INSERT INTO approval_receipts(
                        approval_id, decision_id, receipt_json, receipt_digest, consumed_at
                    ) VALUES (?, ?, ?, ?, NULL)
                    """,
                    (
                        approval.approval_id,
                        approval.decision_id,
                        record_to_json(approval),
                        canonical_digest(approval),
                    ),
                )
                self._append_event("approval.issued", approval)
        except sqlite3.IntegrityError as exc:
            raise PersistenceConflict(
                f"Approval already exists: {approval.approval_id}"
            ) from exc

    def get_approval(self, approval_id: str) -> ApprovalReceipt:
        row = self._host_store._db.execute(
            """
            SELECT approval_id, decision_id, receipt_json, receipt_digest, consumed_at
            FROM approval_receipts WHERE approval_id = ?
            """,
            (approval_id,),
        ).fetchone()
        if row is None:
            raise ApprovalInvalid(f"unknown approval: {approval_id}")
        if row["consumed_at"] is not None:
            raise ApprovalConsumed(f"approval already consumed: {approval_id}")
        try:
            approval = record_from_json(ApprovalReceipt, row["receipt_json"])
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("approval cannot be decoded") from exc
        if approval.approval_id != row["approval_id"] or approval.decision_id != row["decision_id"]:
            raise IntegrityViolation("approval row identity mismatch")
        if canonical_digest(approval) != row["receipt_digest"]:
            raise IntegrityViolation("approval digest mismatch")
        return approval

    def consume_approval(self, approval_id: str) -> None:
        with self._host_store._transaction():
            approval = self.get_approval(approval_id)
            cursor = self._host_store._db.execute(
                """
                UPDATE approval_receipts SET consumed_at = ?
                WHERE approval_id = ? AND consumed_at IS NULL
                """,
                (utc_now(), approval_id),
            )
            if cursor.rowcount != 1:
                raise ApprovalConsumed(f"approval already consumed: {approval_id}")
            self._append_event("approval.consumed", approval)

    def record_execution_authority(self, receipt: ExecutionAuthorityReceipt) -> None:
        try:
            with self._host_store._transaction():
                self._host_store._db.execute(
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
                self._append_event("authority.execution_issued", receipt)
        except sqlite3.IntegrityError as exc:
            raise PersistenceConflict(
                f"execution authority identity already exists: {receipt.receipt_id}"
            ) from exc

    def get_execution_authority(self, receipt_id: str) -> ExecutionAuthorityReceipt:
        row = self._host_store._db.execute(
            """
            SELECT receipt_id, single_use_identity, receipt_json, receipt_digest, consumed_at
            FROM execution_authority_receipts WHERE receipt_id = ?
            """,
            (receipt_id,),
        ).fetchone()
        if row is None:
            raise InvalidRequest(f"unknown execution authority: {receipt_id}")
        if row["consumed_at"] is not None:
            raise AuthorityDenied("execution authority is already consumed")
        try:
            receipt = record_from_json(ExecutionAuthorityReceipt, row["receipt_json"])
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("execution authority cannot be decoded") from exc
        if receipt.receipt_id != row["receipt_id"]:
            raise IntegrityViolation("execution authority row identity mismatch")
        if receipt.single_use_identity != row["single_use_identity"]:
            raise IntegrityViolation("execution authority single-use identity mismatch")
        if canonical_digest(receipt) != row["receipt_digest"]:
            raise IntegrityViolation("execution authority digest mismatch")
        return receipt

    def consume_execution_authority(self, receipt_id: str) -> None:
        with self._host_store._transaction():
            receipt = self.get_execution_authority(receipt_id)
            cursor = self._host_store._db.execute(
                """
                UPDATE execution_authority_receipts SET consumed_at = ?
                WHERE receipt_id = ? AND consumed_at IS NULL
                """,
                (utc_now(), receipt_id),
            )
            if cursor.rowcount != 1:
                raise AuthorityDenied("execution authority is already consumed")
            self._append_event("authority.execution_consumed", receipt)
