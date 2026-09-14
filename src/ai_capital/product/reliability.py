from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from ..kernel.durable_program import ProgramRepository
from ..kernel.errors import IntegrityViolation, InvalidRequest, PersistenceConflict
from ..kernel.events import utc_now
from ..kernel.serialization import canonical_digest, canonical_json


_COMPONENT = "product_reliability"
_COMPONENT_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class ProductRequestRecord:
    request_id: str
    payload: dict[str, Any]
    state: str
    decision_id: str | None
    result: dict[str, Any] | None
    created_at: str
    updated_at: str


class ProductRequestRepository:
    """Durable duplicate-request truth for the local product boundary."""

    def __init__(self, programs: ProgramRepository):
        self._programs = programs
        self._migrate()

    def _migrate(self) -> None:
        with self._programs._transaction():
            self._programs._db.execute(
                """
                CREATE TABLE IF NOT EXISTS component_schema (
                    component TEXT PRIMARY KEY,
                    version INTEGER NOT NULL
                )
                """
            )
            row = self._programs._db.execute(
                "SELECT version FROM component_schema WHERE component = ?",
                (_COMPONENT,),
            ).fetchone()
            version = None if row is None else int(row[0])
            if version is not None and version != _COMPONENT_SCHEMA_VERSION:
                raise IntegrityViolation(
                    f"unsupported product reliability schema version {version}"
                )
            if version is None:
                self._programs._db.execute(
                    """
                    CREATE TABLE product_capability_requests (
                        request_id TEXT PRIMARY KEY,
                        payload_json TEXT NOT NULL,
                        payload_digest TEXT NOT NULL,
                        state TEXT NOT NULL,
                        decision_id TEXT,
                        result_json TEXT,
                        result_digest TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                self._programs._db.execute(
                    "INSERT INTO component_schema(component, version) VALUES (?, ?)",
                    (_COMPONENT, _COMPONENT_SCHEMA_VERSION),
                )
            self._validate_schema()

    def _validate_schema(self) -> None:
        columns = tuple(
            str(row[1])
            for row in self._programs._db.execute(
                "PRAGMA table_info(product_capability_requests)"
            ).fetchall()
        )
        expected = (
            "request_id",
            "payload_json",
            "payload_digest",
            "state",
            "decision_id",
            "result_json",
            "result_digest",
            "created_at",
            "updated_at",
        )
        if columns != expected:
            raise IntegrityViolation("product request table shape mismatch")

    @staticmethod
    def _validate_request_id(request_id: str) -> str:
        if type(request_id) is not str or not request_id.strip():
            raise InvalidRequest("request_id must be non-empty")
        return request_id

    @staticmethod
    def _canonical_object(value: object, *, field: str) -> tuple[str, str]:
        if type(value) is not dict:
            raise InvalidRequest(f"{field} must be an object")
        encoded = canonical_json(value)
        return encoded, canonical_digest(value)

    def _decode(self, row: sqlite3.Row) -> ProductRequestRecord:
        request_id = str(row["request_id"])
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise IntegrityViolation("product request payload cannot be decoded") from exc
        if type(payload) is not dict:
            raise IntegrityViolation("product request payload is not an object")
        if canonical_digest(payload) != row["payload_digest"]:
            raise IntegrityViolation("product request payload digest mismatch")

        state = str(row["state"])
        if state not in {"pending", "completed"}:
            raise IntegrityViolation("product request state is invalid")

        result: dict[str, Any] | None = None
        if state == "completed":
            if row["result_json"] is None or row["result_digest"] is None:
                raise IntegrityViolation("completed product request lacks result")
            try:
                candidate = json.loads(row["result_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise IntegrityViolation("product request result cannot be decoded") from exc
            if type(candidate) is not dict:
                raise IntegrityViolation("product request result is not an object")
            if canonical_digest(candidate) != row["result_digest"]:
                raise IntegrityViolation("product request result digest mismatch")
            result = candidate
        elif row["result_json"] is not None or row["result_digest"] is not None:
            raise IntegrityViolation("pending product request unexpectedly has a result")

        created_at = str(row["created_at"])
        updated_at = str(row["updated_at"])
        if not created_at or not updated_at:
            raise IntegrityViolation("product request timestamps are invalid")
        decision_id = None if row["decision_id"] is None else str(row["decision_id"])
        return ProductRequestRecord(
            request_id=request_id,
            payload=payload,
            state=state,
            decision_id=decision_id,
            result=result,
            created_at=created_at,
            updated_at=updated_at,
        )

    def get(self, request_id: str) -> ProductRequestRecord:
        request_id = self._validate_request_id(request_id)
        row = self._programs._db.execute(
            """
            SELECT request_id, payload_json, payload_digest, state, decision_id,
                   result_json, result_digest, created_at, updated_at
            FROM product_capability_requests WHERE request_id = ?
            """,
            (request_id,),
        ).fetchone()
        if row is None:
            raise InvalidRequest(f"unknown product request: {request_id}")
        return self._decode(row)

    def begin(self, request_id: str, payload: dict[str, Any]) -> ProductRequestRecord:
        request_id = self._validate_request_id(request_id)
        payload_json, payload_digest = self._canonical_object(payload, field="request payload")
        now = utc_now()
        try:
            with self._programs._transaction():
                row = self._programs._db.execute(
                    """
                    SELECT request_id, payload_json, payload_digest, state, decision_id,
                           result_json, result_digest, created_at, updated_at
                    FROM product_capability_requests WHERE request_id = ?
                    """,
                    (request_id,),
                ).fetchone()
                if row is None:
                    self._programs._db.execute(
                        """
                        INSERT INTO product_capability_requests(
                            request_id, payload_json, payload_digest, state, decision_id,
                            result_json, result_digest, created_at, updated_at
                        ) VALUES (?, ?, ?, 'pending', NULL, NULL, NULL, ?, ?)
                        """,
                        (request_id, payload_json, payload_digest, now, now),
                    )
                else:
                    existing = self._decode(row)
                    if row["payload_json"] != payload_json or row["payload_digest"] != payload_digest:
                        raise InvalidRequest(
                            "request_id is already bound to a different invocation payload"
                        )
                    return existing
        except sqlite3.IntegrityError as exc:
            raise PersistenceConflict("product request identity collision") from exc
        return self.get(request_id)

    def bind_decision(self, request_id: str, decision_id: str) -> ProductRequestRecord:
        request_id = self._validate_request_id(request_id)
        if type(decision_id) is not str or not decision_id.strip():
            raise InvalidRequest("decision_id must be non-empty")
        with self._programs._transaction():
            current = self.get(request_id)
            if current.state != "pending":
                raise PersistenceConflict("completed product request cannot change decision")
            if current.decision_id is not None and current.decision_id != decision_id:
                raise PersistenceConflict("product request is already bound to another decision")
            self._programs._db.execute(
                """
                UPDATE product_capability_requests
                SET decision_id = ?, updated_at = ?
                WHERE request_id = ? AND state = 'pending'
                """,
                (decision_id, utc_now(), request_id),
            )
        return self.get(request_id)

    def complete(self, request_id: str, result: dict[str, Any]) -> ProductRequestRecord:
        request_id = self._validate_request_id(request_id)
        result_json, result_digest = self._canonical_object(result, field="request result")
        with self._programs._transaction():
            current = self.get(request_id)
            if current.state == "completed":
                if canonical_json(current.result) != result_json:
                    raise PersistenceConflict("completed product request result changed")
                return current
            cursor = self._programs._db.execute(
                """
                UPDATE product_capability_requests
                SET state = 'completed', result_json = ?, result_digest = ?, updated_at = ?
                WHERE request_id = ? AND state = 'pending'
                """,
                (result_json, result_digest, utc_now(), request_id),
            )
            if cursor.rowcount != 1:
                raise PersistenceConflict("product request changed during completion")
        return self.get(request_id)

    def pending(self) -> tuple[ProductRequestRecord, ...]:
        rows = self._programs._db.execute(
            """
            SELECT request_id, payload_json, payload_digest, state, decision_id,
                   result_json, result_digest, created_at, updated_at
            FROM product_capability_requests
            WHERE state = 'pending' ORDER BY request_id
            """
        ).fetchall()
        return tuple(self._decode(row) for row in rows)
