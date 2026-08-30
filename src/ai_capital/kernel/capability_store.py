from __future__ import annotations

import sqlite3
from dataclasses import replace
from uuid import uuid4

from .durable_program import ProgramRepository
from .errors import (
    IntegrityViolation,
    InvalidRequest,
    PersistenceConflict,
    StaleCapabilityBinding,
    UnknownCapability,
)
from .events import utc_now
from .models import Capability, CapabilityDescriptor, CapabilitySnapshot
from .schema_codec import record_from_json, record_to_json
from .serialization import canonical_digest
from .structured_schema import validate_schema_definition


_COMPONENT = "capability_registry"
_COMPONENT_SCHEMA_VERSION = 1


def capability_descriptor(capability: Capability) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        capability_id=capability.capability_id,
        schema_version=capability.schema_version,
        operation=capability.operation,
        resource_type=capability.resource_type,
        effect_class=capability.effect_class,
        reversibility=capability.reversibility,
        risk_class=capability.risk_class,
        input_schema=capability.input_schema,
        output_schema=capability.output_schema,
        binding_revision=capability.binding_revision,
    )


class CapabilityRepository:
    """Durable semantic capability registry in the Host-owned local store."""

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
                        f"Capability schema version {version} is newer than supported "
                        f"{_COMPONENT_SCHEMA_VERSION}"
                    )
                if version != _COMPONENT_SCHEMA_VERSION:
                    raise IntegrityViolation(
                        f"unsupported Capability schema version {version}"
                    )
                return

            statements = (
                """
                CREATE TABLE IF NOT EXISTS capability_projections (
                    capability_id TEXT PRIMARY KEY,
                    binding_revision INTEGER NOT NULL,
                    capability_json TEXT NOT NULL,
                    capability_digest TEXT NOT NULL
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS capability_bindings (
                    capability_id TEXT NOT NULL,
                    binding_revision INTEGER NOT NULL,
                    capability_json TEXT NOT NULL,
                    capability_digest TEXT NOT NULL,
                    PRIMARY KEY(capability_id, binding_revision)
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS capability_snapshots (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    snapshot_id TEXT NOT NULL UNIQUE,
                    snapshot_json TEXT NOT NULL,
                    snapshot_digest TEXT NOT NULL
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
    def _validate_capability(capability: Capability, *, new: bool) -> None:
        if not capability.capability_id.strip():
            raise InvalidRequest("Capability identity must be non-empty")
        if capability.schema_version < 1:
            raise InvalidRequest("Capability schema version must be positive")
        if not capability.operation.strip() or not capability.resource_type.strip():
            raise InvalidRequest("Capability operation and resource type must be non-empty")
        if not capability.handler_binding.strip():
            raise InvalidRequest("Capability handler binding must be non-empty")
        if new and capability.binding_revision != 0:
            raise InvalidRequest("new Capability must begin at binding revision 0")
        validate_schema_definition(capability.input_schema, path="$input_schema")
        validate_schema_definition(capability.output_schema, path="$output_schema")

    def _capability_from_row(self, row: sqlite3.Row) -> Capability:
        try:
            capability = record_from_json(Capability, row["capability_json"])
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("Capability projection cannot be decoded") from exc
        if not isinstance(capability, Capability):
            raise IntegrityViolation("decoded Capability projection has wrong type")
        if capability.capability_id != row["capability_id"]:
            raise IntegrityViolation("Capability projection identity mismatch")
        if capability.binding_revision != int(row["binding_revision"]):
            raise IntegrityViolation("Capability projection binding revision mismatch")
        if canonical_digest(capability) != row["capability_digest"]:
            raise IntegrityViolation("Capability projection digest mismatch")
        try:
            self._validate_capability(capability, new=False)
        except InvalidRequest as exc:
            raise IntegrityViolation("durable Capability contract is invalid") from exc
        return capability

    def register(self, capability: Capability) -> Capability:
        self._validate_capability(capability, new=True)
        encoded = record_to_json(capability)
        digest = canonical_digest(capability)
        try:
            with self._host_store._transaction():
                self._host_store._db.execute(
                    """
                    INSERT INTO capability_bindings(
                        capability_id, binding_revision, capability_json, capability_digest
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        capability.capability_id,
                        capability.binding_revision,
                        encoded,
                        digest,
                    ),
                )
                self._host_store._db.execute(
                    """
                    INSERT INTO capability_projections(
                        capability_id, binding_revision, capability_json, capability_digest
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        capability.capability_id,
                        capability.binding_revision,
                        encoded,
                        digest,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise PersistenceConflict(
                f"Capability already exists: {capability.capability_id}"
            ) from exc
        return capability

    def get(self, capability_id: str) -> Capability:
        row = self._host_store._db.execute(
            """
            SELECT capability_id, binding_revision, capability_json, capability_digest
            FROM capability_projections WHERE capability_id = ?
            """,
            (capability_id,),
        ).fetchone()
        if row is None:
            raise UnknownCapability(capability_id)
        return self._capability_from_row(row)

    def all(self) -> tuple[Capability, ...]:
        rows = self._host_store._db.execute(
            """
            SELECT capability_id, binding_revision, capability_json, capability_digest
            FROM capability_projections ORDER BY capability_id
            """
        ).fetchall()
        return tuple(self._capability_from_row(row) for row in rows)

    def replace_handler(
        self,
        capability_id: str,
        handler_binding: str,
        *,
        expected_binding_revision: int,
    ) -> Capability:
        if not handler_binding.strip():
            raise InvalidRequest("Capability handler binding must be non-empty")
        with self._host_store._transaction():
            current = self.get(capability_id)
            if current.binding_revision != expected_binding_revision:
                raise StaleCapabilityBinding(
                    f"expected binding revision {expected_binding_revision}, "
                    f"current revision {current.binding_revision}"
                )
            updated = replace(
                current,
                handler_binding=handler_binding,
                binding_revision=current.binding_revision + 1,
            )
            self._validate_capability(updated, new=False)
            encoded = record_to_json(updated)
            digest = canonical_digest(updated)
            self._host_store._db.execute(
                """
                INSERT INTO capability_bindings(
                    capability_id, binding_revision, capability_json, capability_digest
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    updated.capability_id,
                    updated.binding_revision,
                    encoded,
                    digest,
                ),
            )
            cursor = self._host_store._db.execute(
                """
                UPDATE capability_projections
                SET binding_revision = ?, capability_json = ?, capability_digest = ?
                WHERE capability_id = ? AND binding_revision = ?
                """,
                (
                    updated.binding_revision,
                    encoded,
                    digest,
                    capability_id,
                    expected_binding_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise StaleCapabilityBinding(
                    f"Capability binding changed during replacement: {capability_id}"
                )
            return updated

    def bindings(self, capability_id: str) -> tuple[Capability, ...]:
        rows = self._host_store._db.execute(
            """
            SELECT capability_id, binding_revision, capability_json, capability_digest
            FROM capability_bindings
            WHERE capability_id = ? ORDER BY binding_revision
            """,
            (capability_id,),
        ).fetchall()
        if not rows:
            raise UnknownCapability(capability_id)
        capabilities = tuple(self._capability_from_row(row) for row in rows)
        for expected, capability in enumerate(capabilities):
            if capability.binding_revision != expected:
                raise IntegrityViolation("Capability binding revisions are not contiguous")
        return capabilities

    def create_snapshot(
        self,
        descriptors: tuple[CapabilityDescriptor, ...],
    ) -> CapabilitySnapshot:
        ordered = tuple(sorted(descriptors, key=lambda item: item.capability_id))
        if len({item.capability_id for item in ordered}) != len(ordered):
            raise InvalidRequest("Capability snapshot contains duplicate identities")
        snapshot = CapabilitySnapshot(
            snapshot_id=str(uuid4()),
            capabilities=ordered,
            created_at=utc_now(),
        )
        with self._host_store._transaction():
            self._host_store._db.execute(
                """
                INSERT INTO capability_snapshots(
                    snapshot_id, snapshot_json, snapshot_digest
                ) VALUES (?, ?, ?)
                """,
                (
                    snapshot.snapshot_id,
                    record_to_json(snapshot),
                    canonical_digest(snapshot),
                ),
            )
        return snapshot

    def get_snapshot(self, snapshot_id: str) -> CapabilitySnapshot:
        row = self._host_store._db.execute(
            """
            SELECT snapshot_json, snapshot_digest
            FROM capability_snapshots WHERE snapshot_id = ?
            """,
            (snapshot_id,),
        ).fetchone()
        if row is None:
            raise InvalidRequest(f"unknown Capability snapshot: {snapshot_id}")
        try:
            snapshot = record_from_json(CapabilitySnapshot, row["snapshot_json"])
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("Capability snapshot cannot be decoded") from exc
        if not isinstance(snapshot, CapabilitySnapshot):
            raise IntegrityViolation("decoded Capability snapshot has wrong type")
        if canonical_digest(snapshot) != row["snapshot_digest"]:
            raise IntegrityViolation("Capability snapshot digest mismatch")
        if tuple(sorted(snapshot.capabilities, key=lambda item: item.capability_id)) != snapshot.capabilities:
            raise IntegrityViolation("Capability snapshot is not canonically ordered")
        if len({item.capability_id for item in snapshot.capabilities}) != len(snapshot.capabilities):
            raise IntegrityViolation("Capability snapshot contains duplicate identities")
        return snapshot
