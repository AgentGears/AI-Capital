from __future__ import annotations

import sqlite3

from .actor_state import replace_model_binding
from .durable_program import ProgramRepository
from .enums import ActorStatus, ModelAttemptOutcome
from .errors import IntegrityViolation, InvalidRequest, PersistenceConflict, StaleActorGeneration
from .models import Actor, ModelAttemptReceipt, ModelTurn
from .schema_codec import record_from_json, record_to_json
from .serialization import canonical_digest


_COMPONENT = "actor_inference"
_COMPONENT_SCHEMA_VERSION = 1


class ActorRepository:
    """Durable Actor identity and inference-attempt state in the Host store."""

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
                        f"Actor schema version {version} is newer than supported "
                        f"{_COMPONENT_SCHEMA_VERSION}"
                    )
                if version != _COMPONENT_SCHEMA_VERSION:
                    raise IntegrityViolation(f"unsupported Actor schema version {version}")
                return

            self._host_store._db.executescript(
                """
                CREATE TABLE actor_projections (
                    actor_id TEXT PRIMARY KEY,
                    generation INTEGER NOT NULL,
                    actor_json TEXT NOT NULL,
                    actor_digest TEXT NOT NULL
                );
                CREATE TABLE actor_generations (
                    actor_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    actor_json TEXT NOT NULL,
                    actor_digest TEXT NOT NULL,
                    PRIMARY KEY(actor_id, generation)
                );
                CREATE TABLE model_attempts (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    attempt_id TEXT NOT NULL UNIQUE,
                    actor_id TEXT NOT NULL,
                    actor_generation INTEGER NOT NULL,
                    program_id TEXT NOT NULL,
                    program_revision INTEGER NOT NULL,
                    receipt_json TEXT NOT NULL,
                    receipt_digest TEXT NOT NULL,
                    turn_json TEXT,
                    turn_digest TEXT
                );
                CREATE INDEX model_attempts_actor_sequence
                    ON model_attempts(actor_id, sequence);
                CREATE INDEX model_attempts_program_sequence
                    ON model_attempts(program_id, sequence);
                """
            )
            self._host_store._db.execute(
                "INSERT INTO component_schema(component, version) VALUES (?, ?)",
                (_COMPONENT, _COMPONENT_SCHEMA_VERSION),
            )

    def _actor_from_row(self, row: sqlite3.Row) -> Actor:
        try:
            actor = record_from_json(Actor, row["actor_json"])
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("Actor projection cannot be decoded") from exc
        if not isinstance(actor, Actor):
            raise IntegrityViolation("decoded Actor projection has wrong type")
        if actor.actor_id != row["actor_id"]:
            raise IntegrityViolation("Actor projection identity mismatch")
        if actor.generation != int(row["generation"]):
            raise IntegrityViolation("Actor projection generation mismatch")
        if canonical_digest(actor) != row["actor_digest"]:
            raise IntegrityViolation("Actor projection digest mismatch")
        return actor

    def _read_actor_row(self, actor_id: str) -> sqlite3.Row:
        row = self._host_store._db.execute(
            """
            SELECT actor_id, generation, actor_json, actor_digest
            FROM actor_projections WHERE actor_id = ?
            """,
            (actor_id,),
        ).fetchone()
        if row is None:
            raise InvalidRequest(f"unknown Actor: {actor_id}")
        return row

    def register(self, actor: Actor) -> Actor:
        if actor.generation != 0:
            raise InvalidRequest("new Actor must begin at generation 0")
        if actor.status is not ActorStatus.ACTIVE:
            raise InvalidRequest("new Actor must begin active")
        if not actor.actor_id.strip() or not actor.model_binding.strip():
            raise InvalidRequest("Actor identity and model binding must be non-empty")
        encoded = record_to_json(actor)
        digest = canonical_digest(actor)
        try:
            with self._host_store._transaction():
                self._host_store._db.execute(
                    """
                    INSERT INTO actor_generations(
                        actor_id, generation, actor_json, actor_digest
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (actor.actor_id, actor.generation, encoded, digest),
                )
                self._host_store._db.execute(
                    """
                    INSERT INTO actor_projections(
                        actor_id, generation, actor_json, actor_digest
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (actor.actor_id, actor.generation, encoded, digest),
                )
        except sqlite3.IntegrityError as exc:
            raise PersistenceConflict(f"Actor already exists: {actor.actor_id}") from exc
        return actor

    def get(self, actor_id: str) -> Actor:
        return self._actor_from_row(self._read_actor_row(actor_id))

    def replace_binding(
        self,
        actor_id: str,
        new_model_binding: str,
        *,
        expected_generation: int,
    ) -> Actor:
        with self._host_store._transaction():
            current = self._actor_from_row(self._read_actor_row(actor_id))
            updated = replace_model_binding(
                current,
                new_model_binding,
                expected_generation=expected_generation,
            )
            encoded = record_to_json(updated)
            digest = canonical_digest(updated)
            self._host_store._db.execute(
                """
                INSERT INTO actor_generations(
                    actor_id, generation, actor_json, actor_digest
                ) VALUES (?, ?, ?, ?)
                """,
                (updated.actor_id, updated.generation, encoded, digest),
            )
            cursor = self._host_store._db.execute(
                """
                UPDATE actor_projections
                SET generation = ?, actor_json = ?, actor_digest = ?
                WHERE actor_id = ? AND generation = ?
                """,
                (
                    updated.generation,
                    encoded,
                    digest,
                    actor_id,
                    expected_generation,
                ),
            )
            if cursor.rowcount != 1:
                raise StaleActorGeneration(
                    f"Actor generation changed during replacement: {actor_id}"
                )
            return updated

    def generations(self, actor_id: str) -> tuple[Actor, ...]:
        rows = self._host_store._db.execute(
            """
            SELECT actor_id, generation, actor_json, actor_digest
            FROM actor_generations
            WHERE actor_id = ?
            ORDER BY generation
            """,
            (actor_id,),
        ).fetchall()
        if not rows:
            raise InvalidRequest(f"unknown Actor: {actor_id}")
        actors = tuple(self._actor_from_row(row) for row in rows)
        for expected, actor in enumerate(actors):
            if actor.generation != expected:
                raise IntegrityViolation("Actor generations are not contiguous")
        return actors

    def record_attempt(
        self,
        receipt: ModelAttemptReceipt,
        turn: ModelTurn | None,
    ) -> None:
        generation = self._host_store._db.execute(
            """
            SELECT 1 FROM actor_generations
            WHERE actor_id = ? AND generation = ?
            """,
            (receipt.actor_id, receipt.actor_generation),
        ).fetchone()
        if generation is None:
            raise IntegrityViolation("model attempt references unknown Actor generation")

        if receipt.outcome is ModelAttemptOutcome.SUCCEEDED:
            if turn is None or receipt.error_code is not None:
                raise IntegrityViolation("successful model attempt requires output and no error")
            if turn.provenance_receipt != receipt.attempt_id:
                raise IntegrityViolation("model output provenance does not match attempt")
            turn_digest = canonical_digest(turn)
            if receipt.output_digest != turn_digest:
                raise IntegrityViolation("model output digest does not match receipt")
            turn_json = record_to_json(turn)
        else:
            if turn is not None or receipt.output_digest is not None:
                raise IntegrityViolation("failed model attempt cannot carry accepted output")
            if not receipt.error_code:
                raise IntegrityViolation("failed model attempt requires an AI Capital error code")
            turn_digest = None
            turn_json = None

        try:
            with self._host_store._transaction():
                self._host_store._db.execute(
                    """
                    INSERT INTO model_attempts(
                        attempt_id, actor_id, actor_generation,
                        program_id, program_revision,
                        receipt_json, receipt_digest, turn_json, turn_digest
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        receipt.attempt_id,
                        receipt.actor_id,
                        receipt.actor_generation,
                        receipt.program_id,
                        receipt.program_revision,
                        record_to_json(receipt),
                        canonical_digest(receipt),
                        turn_json,
                        turn_digest,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise PersistenceConflict(
                f"duplicate model attempt identity: {receipt.attempt_id}"
            ) from exc

    def attempts(self, actor_id: str) -> tuple[ModelAttemptReceipt, ...]:
        rows = self._host_store._db.execute(
            """
            SELECT receipt_json, receipt_digest
            FROM model_attempts
            WHERE actor_id = ?
            ORDER BY sequence
            """,
            (actor_id,),
        ).fetchall()
        receipts: list[ModelAttemptReceipt] = []
        for row in rows:
            try:
                receipt = record_from_json(ModelAttemptReceipt, row["receipt_json"])
            except (TypeError, ValueError) as exc:
                raise IntegrityViolation("model attempt receipt cannot be decoded") from exc
            if not isinstance(receipt, ModelAttemptReceipt):
                raise IntegrityViolation("decoded model attempt receipt has wrong type")
            if canonical_digest(receipt) != row["receipt_digest"]:
                raise IntegrityViolation("model attempt receipt digest mismatch")
            receipts.append(receipt)
        return tuple(receipts)

    def turn(self, attempt_id: str) -> ModelTurn | None:
        row = self._host_store._db.execute(
            """
            SELECT turn_json, turn_digest
            FROM model_attempts WHERE attempt_id = ?
            """,
            (attempt_id,),
        ).fetchone()
        if row is None:
            raise InvalidRequest(f"unknown model attempt: {attempt_id}")
        if row["turn_json"] is None:
            if row["turn_digest"] is not None:
                raise IntegrityViolation("failed model attempt has unexpected output digest")
            return None
        try:
            turn = record_from_json(ModelTurn, row["turn_json"])
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("model output cannot be decoded") from exc
        if not isinstance(turn, ModelTurn):
            raise IntegrityViolation("decoded model output has wrong type")
        if canonical_digest(turn) != row["turn_digest"]:
            raise IntegrityViolation("model output digest mismatch")
        return turn
