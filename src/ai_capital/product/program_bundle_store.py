from __future__ import annotations

import sqlite3
from typing import Any

from ..kernel.durable_program import ProgramRepository
from ..kernel.errors import IntegrityViolation, InvalidRequest, PersistenceConflict
from ..kernel.events import utc_now
from ..kernel.operation_journal import OperationJournal
from ..kernel.program_control import ProgramControlRepository
from ..kernel.serialization import canonical_digest, to_canonical_data
from .audit_operator import LocalAuditOperator
from .program_bundle_auth import build_operation_audit_auth
from .program_bundle_codec import embedded_artifact, encode_bundle, validate_bundle
from .workspace_snapshots import WorkspaceSnapshotStore
from .workspace_types import WorkspaceArtifact, require_text, sha256_bytes, validate_timestamp


class ProgramBundleStore:
    """Deterministic export and non-authoritative imported Program archives."""

    def __init__(
        self,
        programs: ProgramRepository,
        snapshots: WorkspaceSnapshotStore,
    ):
        self._programs = programs
        self._snapshots = snapshots
        self._controls = ProgramControlRepository(programs)
        self._operations = OperationJournal(programs)
        self._audit = LocalAuditOperator(programs, self._operations)

    def _payload(self, program_id: str, snapshot_id: str) -> dict[str, Any]:
        self._programs.verify_integrity(program_id)
        program = self._programs.get(program_id)
        self._controls.verify_integrity(program_id)
        control = self._controls.get(program_id)
        snapshot, entries = self._snapshots.record(snapshot_id)
        if snapshot.program_id != program_id:
            raise InvalidRequest("workspace snapshot belongs to a different Program")
        if snapshot.program_revision != program.revision:
            raise InvalidRequest(
                "workspace snapshot is not bound to the current Program revision"
            )
        events = self._programs.list_events(program_id)
        artifacts = tuple(
            embedded_artifact(
                entry,
                self._snapshots.artifact_bytes_by_entry(entry),
            )
            for entry in entries
        )
        audit = {
            "asks": self._audit.asks(program_id),
            "operations": tuple(
                self._audit.audit_operation(ref) for ref in program.operation_refs
            ),
            "evidence": tuple(
                self._audit.audit_evidence(ref) for ref in program.evidence_refs
            ),
            "verifications": tuple(
                self._audit.audit_verification(ref)
                for ref in program.verification_refs
            ),
        }
        operation_audit_auth = build_operation_audit_auth(
            self._programs,
            audit["operations"],
        )
        return {
            "source_program_id": program.program_id,
            "program": to_canonical_data(program),
            "program_digest": canonical_digest(program),
            "events": tuple(to_canonical_data(event) for event in events),
            "control": to_canonical_data(control),
            "workspace": {
                "snapshot": to_canonical_data(snapshot),
                "artifacts": artifacts,
            },
            "audit": audit,
            "operation_audit_auth": operation_audit_auth,
        }

    def export(self, program_id: str, snapshot_id: str) -> bytes:
        program_id = require_text(program_id, field="program_id")
        snapshot_id = require_text(snapshot_id, field="snapshot_id")
        return encode_bundle(self._payload(program_id, snapshot_id))

    def import_archive(self, content: bytes) -> dict[str, Any]:
        envelope, program, _, artifacts = validate_bundle(content)
        bundle_id = str(envelope["bundle_id"])
        bundle_json = content.decode("utf-8")
        bundle_digest = sha256_bytes(content)
        existing = self._programs._db.execute(
            """
            SELECT bundle_id, source_program_id, bundle_json, bundle_digest, imported_at
            FROM program_bundle_imports WHERE bundle_id = ?
            """,
            (bundle_id,),
        ).fetchone()
        if existing is not None:
            if (
                existing["source_program_id"] != program.program_id
                or existing["bundle_json"] != bundle_json
                or existing["bundle_digest"] != bundle_digest
            ):
                raise IntegrityViolation("Program bundle identity collision")
            return self.show(bundle_id)

        for entry, exact in artifacts:
            self._snapshots.store_imported_artifact(entry, exact)
        imported_at = utc_now()
        try:
            with self._programs._transaction():
                self._programs._db.execute(
                    """
                    INSERT INTO program_bundle_imports(
                        bundle_id, source_program_id, bundle_json, bundle_digest, imported_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        bundle_id,
                        program.program_id,
                        bundle_json,
                        bundle_digest,
                        imported_at,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise PersistenceConflict("Program bundle import collision") from exc
        return self.show(bundle_id)

    def _row(self, bundle_id: str) -> sqlite3.Row:
        bundle_id = require_text(bundle_id, field="bundle_id")
        row = self._programs._db.execute(
            """
            SELECT bundle_id, source_program_id, bundle_json, bundle_digest, imported_at
            FROM program_bundle_imports WHERE bundle_id = ?
            """,
            (bundle_id,),
        ).fetchone()
        if row is None:
            raise InvalidRequest(f"unknown imported Program bundle: {bundle_id}")
        if type(row["bundle_json"]) is not str or not row["bundle_json"]:
            raise IntegrityViolation("imported Program bundle JSON is malformed")
        content = row["bundle_json"].encode("utf-8")
        if row["bundle_digest"] != sha256_bytes(content):
            raise IntegrityViolation("imported Program bundle digest mismatch")
        if row["bundle_id"] != bundle_id:
            raise IntegrityViolation("imported Program bundle identity mismatch")
        try:
            validate_timestamp(row["imported_at"], field="imported_at")
        except InvalidRequest as exc:
            raise IntegrityViolation("imported Program bundle timestamp is invalid") from exc
        return row

    def show(self, bundle_id: str) -> dict[str, Any]:
        row = self._row(bundle_id)
        content = row["bundle_json"].encode("utf-8")
        envelope, program, snapshot, artifacts = validate_bundle(content)
        if program.program_id != row["source_program_id"]:
            raise IntegrityViolation("imported Program bundle source identity mismatch")
        for entry, exact in artifacts:
            if self._snapshots.artifact_bytes_by_entry(entry) != exact:
                raise IntegrityViolation("imported Program bundle artifact store mismatch")
        return {
            "bundle": envelope,
            "source_program_id": program.program_id,
            "program_revision": program.revision,
            "snapshot_id": snapshot.snapshot_id,
            "artifact_count": len(artifacts),
            "imported_at": row["imported_at"],
        }

    def list(self) -> tuple[dict[str, Any], ...]:
        rows = self._programs._db.execute(
            "SELECT bundle_id FROM program_bundle_imports ORDER BY bundle_id"
        ).fetchall()
        return tuple(self.show(str(row["bundle_id"])) for row in rows)

    def artifacts(self, bundle_id: str) -> tuple[dict[str, Any], ...]:
        view = self.show(bundle_id)
        items = view["bundle"]["payload"]["workspace"]["artifacts"]
        return tuple(item["entry"] for item in items)

    def artifact_bytes(self, bundle_id: str, path: str) -> bytes:
        view = self.show(bundle_id)
        items = view["bundle"]["payload"]["workspace"]["artifacts"]
        matches = tuple(item for item in items if item["entry"]["path"] == path)
        if len(matches) != 1:
            raise InvalidRequest(f"unknown imported bundle artifact: {path}")
        entry_data = matches[0]["entry"]
        entry = WorkspaceArtifact(
            path=str(entry_data["path"]),
            digest=str(entry_data["digest"]),
            byte_length=entry_data["byte_length"],
            content_ref=str(entry_data["content_ref"]),
        )
        return self._snapshots.artifact_bytes_by_entry(entry)
