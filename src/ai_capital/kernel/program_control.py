from __future__ import annotations

import sqlite3
from dataclasses import dataclass, replace

from .durable_program import ProgramRepository
from .enums import ProgramStatus
from .errors import (
    IntegrityViolation,
    InvalidStateTransition,
    PersistenceConflict,
    StaleProgramControlRevision,
    StaleProgramRevision,
)
from .events import utc_now
from .schema_codec import record_from_json, record_to_json
from .serialization import canonical_digest, canonical_json


_COMPONENT = "program_control"
_COMPONENT_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class ProgramControl:
    program_id: str
    revision: int
    paused: bool
    last_reason_code: str | None = None
    changed_at: str | None = None


class ProgramControlRepository:
    """Host-owned durable execution control that is distinct from Program status."""

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
            version = None if row is None else int(row[0])
            if version is not None and version != _COMPONENT_SCHEMA_VERSION:
                raise IntegrityViolation(
                    f"unsupported Program-control schema version {version}"
                )

            self._host_store._db.execute(
                """
                CREATE TABLE IF NOT EXISTS program_control_history (
                    program_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    control_json TEXT NOT NULL,
                    control_digest TEXT NOT NULL,
                    PRIMARY KEY(program_id, revision)
                )
                """
            )
            self._host_store._db.execute(
                """
                CREATE TABLE IF NOT EXISTS program_control_projections (
                    program_id TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL,
                    control_json TEXT NOT NULL,
                    control_digest TEXT NOT NULL
                )
                """
            )
            if version is None:
                self._host_store._db.execute(
                    "INSERT INTO component_schema(component, version) VALUES (?, ?)",
                    (_COMPONENT, _COMPONENT_SCHEMA_VERSION),
                )

    @staticmethod
    def _default(program_id: str) -> ProgramControl:
        return ProgramControl(program_id=program_id, revision=0, paused=False)

    @staticmethod
    def _decode(row: sqlite3.Row) -> ProgramControl:
        try:
            control = record_from_json(ProgramControl, row["control_json"])
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("Program-control record cannot be decoded") from exc
        if not isinstance(control, ProgramControl):
            raise IntegrityViolation("Program-control record decoded wrong type")
        if control.program_id != row["program_id"]:
            raise IntegrityViolation("Program-control identity mismatch")
        if control.revision != int(row["revision"]):
            raise IntegrityViolation("Program-control revision mismatch")
        if canonical_digest(control) != row["control_digest"]:
            raise IntegrityViolation("Program-control digest mismatch")
        return control

    @staticmethod
    def _validate_state(control: ProgramControl) -> None:
        if control.revision < 1:
            raise IntegrityViolation("persisted Program-control revision must be positive")
        if not control.changed_at:
            raise IntegrityViolation("persisted Program control requires changed_at")
        expected_reason = "user_paused" if control.paused else "user_resumed"
        if control.last_reason_code != expected_reason:
            raise IntegrityViolation("Program-control reason does not match pause state")

    def _projection_row(self, program_id: str) -> sqlite3.Row | None:
        return self._host_store._db.execute(
            """
            SELECT program_id, revision, control_json, control_digest
            FROM program_control_projections WHERE program_id = ?
            """,
            (program_id,),
        ).fetchone()

    def _history_rows(self, program_id: str) -> tuple[sqlite3.Row, ...]:
        rows = self._host_store._db.execute(
            """
            SELECT program_id, revision, control_json, control_digest
            FROM program_control_history
            WHERE program_id = ? ORDER BY revision
            """,
            (program_id,),
        ).fetchall()
        return tuple(rows)

    def _verified_history(self, program_id: str) -> tuple[ProgramControl, ...]:
        projection_row = self._projection_row(program_id)
        history_rows = self._history_rows(program_id)
        if projection_row is None:
            if history_rows:
                raise IntegrityViolation("Program-control history lacks current projection")
            return ()
        if not history_rows:
            raise IntegrityViolation("Program-control projection lacks history")

        history: list[ProgramControl] = []
        expected_paused = True
        for expected_revision, row in enumerate(history_rows, start=1):
            control = self._decode(row)
            self._validate_state(control)
            if control.revision != expected_revision:
                raise IntegrityViolation("Program-control history revisions are not contiguous")
            if control.paused is not expected_paused:
                raise IntegrityViolation("Program-control history does not alternate pause/resume")
            history.append(control)
            expected_paused = not expected_paused

        projection = self._decode(projection_row)
        self._validate_state(projection)
        if canonical_json(projection) != canonical_json(history[-1]):
            raise IntegrityViolation("Program-control projection diverges from history")
        return tuple(history)

    def get(self, program_id: str) -> ProgramControl:
        self._host_store.get(program_id)
        history = self._verified_history(program_id)
        return self._default(program_id) if not history else history[-1]

    def history(self, program_id: str) -> tuple[ProgramControl, ...]:
        self._host_store.get(program_id)
        return self._verified_history(program_id)

    def verify_integrity(self, program_id: str) -> None:
        self._host_store.verify_integrity(program_id)
        self._verified_history(program_id)

    @staticmethod
    def _require_program_revision(current_revision: int, expected_revision: int) -> None:
        if current_revision != expected_revision:
            raise StaleProgramRevision(
                f"expected revision {expected_revision}, current revision {current_revision}"
            )

    @staticmethod
    def _require_control_revision(current_revision: int, expected_revision: int) -> None:
        if current_revision != expected_revision:
            raise StaleProgramControlRevision(
                "expected Program-control revision "
                f"{expected_revision}, current revision {current_revision}"
            )

    def _commit(
        self,
        *,
        previous: ProgramControl,
        updated: ProgramControl,
    ) -> ProgramControl:
        self._validate_state(updated)
        encoded = record_to_json(updated)
        digest = canonical_digest(updated)
        try:
            with self._host_store._transaction():
                current_row = self._projection_row(previous.program_id)
                if previous.revision == 0:
                    if current_row is not None:
                        raise PersistenceConflict(
                            "Program-control projection changed before commit"
                        )
                else:
                    if current_row is None or self._decode(current_row) != previous:
                        raise PersistenceConflict(
                            "Program-control projection changed before commit"
                        )

                self._host_store._db.execute(
                    """
                    INSERT INTO program_control_history(
                        program_id, revision, control_json, control_digest
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (updated.program_id, updated.revision, encoded, digest),
                )
                if previous.revision == 0:
                    self._host_store._db.execute(
                        """
                        INSERT INTO program_control_projections(
                            program_id, revision, control_json, control_digest
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (updated.program_id, updated.revision, encoded, digest),
                    )
                else:
                    cursor = self._host_store._db.execute(
                        """
                        UPDATE program_control_projections
                        SET revision = ?, control_json = ?, control_digest = ?
                        WHERE program_id = ? AND revision = ? AND control_digest = ?
                        """,
                        (
                            updated.revision,
                            encoded,
                            digest,
                            previous.program_id,
                            previous.revision,
                            canonical_digest(previous),
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise PersistenceConflict(
                            "Program-control projection changed during commit"
                        )
        except sqlite3.IntegrityError as exc:
            raise PersistenceConflict("Program-control revision collision") from exc
        return updated

    def pause(
        self,
        program_id: str,
        *,
        expected_program_revision: int,
        expected_control_revision: int,
    ) -> ProgramControl:
        self.verify_integrity(program_id)
        program = self._host_store.get(program_id)
        self._require_program_revision(program.revision, expected_program_revision)
        if program.status is not ProgramStatus.ACTIVE:
            raise InvalidStateTransition(
                f"cannot pause Program while {program.status.value}"
            )
        current = self.get(program_id)
        self._require_control_revision(current.revision, expected_control_revision)
        if current.paused:
            raise InvalidStateTransition("Program is already user-paused")
        return self._commit(
            previous=current,
            updated=replace(
                current,
                revision=current.revision + 1,
                paused=True,
                last_reason_code="user_paused",
                changed_at=utc_now(),
            ),
        )

    def resume(
        self,
        program_id: str,
        *,
        expected_program_revision: int,
        expected_control_revision: int,
    ) -> ProgramControl:
        self.verify_integrity(program_id)
        program = self._host_store.get(program_id)
        self._require_program_revision(program.revision, expected_program_revision)
        if program.status is not ProgramStatus.ACTIVE:
            raise InvalidStateTransition(
                f"cannot resume user pause while Program is {program.status.value}"
            )
        current = self.get(program_id)
        self._require_control_revision(current.revision, expected_control_revision)
        if not current.paused:
            raise InvalidStateTransition("Program is not user-paused")
        return self._commit(
            previous=current,
            updated=replace(
                current,
                revision=current.revision + 1,
                paused=False,
                last_reason_code="user_resumed",
                changed_at=utc_now(),
            ),
        )

    def require_runnable(self, program_id: str) -> None:
        control = self.get(program_id)
        if control.paused:
            raise InvalidStateTransition("Program is user-paused")
