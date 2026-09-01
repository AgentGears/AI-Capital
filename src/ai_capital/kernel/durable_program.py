from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Callable, Iterator

from .enums import ProgramStatus, WorkItemStatus
from .errors import IntegrityViolation, InvalidRequest, PersistenceConflict, StaleProgramRevision
from .events import make_program_event, verify_event_digest
from .frozen_json import FrozenMap
from .models import Event, Program, WorkItem
from .program_state import add_work_item, satisfy_work_item, transition_program
from .schema_codec import record_from_json, record_to_json
from .serialization import canonical_digest, canonical_json


_SCHEMA_VERSION = 1


class LocalWriterLock:
    """Process-level single-writer lock for a file-backed local repository."""

    def __init__(self, path: Path):
        self._path = path
        self._file = None

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self._path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise PersistenceConflict("another local Program writer owns this store") from exc
        self._file = handle

    def release(self) -> None:
        if self._file is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None


class ProgramRepository:
    """K1 durable Program authority with atomic Event + projection commits."""

    def __init__(self, database_path: str | Path):
        self._database_path = str(database_path)
        self._writer_lock: LocalWriterLock | None = None
        self._connection: sqlite3.Connection | None = None
        self._closed = False
        try:
            if self._database_path != ":memory:":
                db_path = Path(self._database_path)
                db_path.parent.mkdir(parents=True, exist_ok=True)
                self._writer_lock = LocalWriterLock(
                    db_path.with_name(db_path.name + ".writer.lock")
                )
                self._writer_lock.acquire()

            self._connection = sqlite3.connect(
                self._database_path,
                isolation_level=None,
                check_same_thread=True,
            )
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._migrate()
        except Exception:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            if self._writer_lock is not None:
                self._writer_lock.release()
            raise

    @property
    def _db(self) -> sqlite3.Connection:
        if self._connection is None or self._closed:
            raise PersistenceConflict("Program repository is closed")
        return self._connection

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
        finally:
            if self._writer_lock is not None:
                self._writer_lock.release()

    def __enter__(self) -> "ProgramRepository":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _migrate(self) -> None:
        version = int(self._db.execute("PRAGMA user_version").fetchone()[0])
        if version > _SCHEMA_VERSION:
            raise IntegrityViolation(
                f"store schema version {version} is newer than supported {_SCHEMA_VERSION}"
            )
        if version == 0:
            self._db.executescript(
                """
                BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS program_projections (
                    program_id TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL,
                    projection_json TEXT NOT NULL,
                    projection_digest TEXT NOT NULL,
                    last_sequence INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY,
                    event_id TEXT NOT NULL UNIQUE,
                    program_id TEXT,
                    event_type TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    event_digest TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_program_sequence
                    ON events(program_id, sequence);
                PRAGMA user_version = 1;
                COMMIT;
                """
            )
            version = 1
        if version != _SCHEMA_VERSION:
            raise IntegrityViolation(f"unsupported store schema version {version}")

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self._db.execute("ROLLBACK")
            raise
        else:
            self._db.execute("COMMIT")

    def _fault(self, stage: str) -> None:
        """Fault-injection seam used by tests; production implementation is inert."""

    def _next_sequence(self) -> int:
        row = self._db.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM events"
        ).fetchone()
        return int(row[0])

    def _insert_event(self, event: Event) -> None:
        try:
            self._db.execute(
                """
                INSERT INTO events(
                    sequence, event_id, program_id, event_type, event_json, event_digest
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event.sequence,
                    event.event_id,
                    event.program_id,
                    event.event_type,
                    record_to_json(event),
                    event.digest,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise PersistenceConflict(
                f"duplicate or conflicting Event identity: {event.event_id}"
            ) from exc

    def _read_projection_row(self, program_id: str) -> sqlite3.Row:
        row = self._db.execute(
            """
            SELECT program_id, revision, projection_json, projection_digest, last_sequence
            FROM program_projections WHERE program_id = ?
            """,
            (program_id,),
        ).fetchone()
        if row is None:
            raise InvalidRequest(f"unknown Program: {program_id}")
        return row

    def _program_from_row(self, row: sqlite3.Row) -> Program:
        try:
            program = record_from_json(Program, row["projection_json"])
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("Program projection cannot be decoded") from exc
        if not isinstance(program, Program):
            raise IntegrityViolation("decoded projection is not a Program")
        if program.program_id != row["program_id"]:
            raise IntegrityViolation("Program projection identity mismatch")
        if program.revision != int(row["revision"]):
            raise IntegrityViolation("Program projection revision mismatch")
        if canonical_digest(program) != row["projection_digest"]:
            raise IntegrityViolation("Program projection digest mismatch")
        return program

    def create(
        self,
        program: Program,
        *,
        event_id: str | None = None,
        occurred_at: str | None = None,
        recorded_at: str | None = None,
    ) -> Program:
        if program.revision != 0 or program.status is not ProgramStatus.CREATED:
            raise InvalidRequest("new Program must begin at revision 0 in created state")
        with self._transaction():
            projection_exists = self._db.execute(
                "SELECT 1 FROM program_projections WHERE program_id = ?",
                (program.program_id,),
            ).fetchone()
            history_exists = self._db.execute(
                "SELECT 1 FROM events WHERE program_id = ? LIMIT 1",
                (program.program_id,),
            ).fetchone()
            if projection_exists is not None or history_exists is not None:
                raise PersistenceConflict(
                    f"Program identity already has durable state: {program.program_id}"
                )
            sequence = self._next_sequence()
            event = make_program_event(
                sequence=sequence,
                event_type="program.created",
                program=program,
                event_id=event_id,
                occurred_at=occurred_at,
                recorded_at=recorded_at,
            )
            self._insert_event(event)
            self._fault("after_event_append")
            self._db.execute(
                """
                INSERT INTO program_projections(
                    program_id, revision, projection_json, projection_digest, last_sequence
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    program.program_id,
                    program.revision,
                    record_to_json(program),
                    canonical_digest(program),
                    sequence,
                ),
            )
            self._fault("after_projection_write")
        self._fault("after_commit")
        return program

    def get(self, program_id: str) -> Program:
        return self._program_from_row(self._read_projection_row(program_id))

    def _commit_change(
        self,
        *,
        program_id: str,
        expected_revision: int,
        event_type: str,
        mutate: Callable[[Program], Program],
        event_id: str | None,
        occurred_at: str | None,
        recorded_at: str | None,
    ) -> Program:
        updated: Program
        with self._transaction():
            row = self._read_projection_row(program_id)
            current = self._program_from_row(row)
            if current.revision != expected_revision:
                raise StaleProgramRevision(
                    f"expected revision {expected_revision}, current revision {current.revision}"
                )
            updated = mutate(current)
            if updated.program_id != current.program_id:
                raise IntegrityViolation("Program mutation changed canonical identity")
            if updated.revision != current.revision + 1:
                raise IntegrityViolation("Program mutation must advance exactly one revision")

            sequence = self._next_sequence()
            event = make_program_event(
                sequence=sequence,
                event_type=event_type,
                program=updated,
                event_id=event_id,
                occurred_at=occurred_at,
                recorded_at=recorded_at,
            )
            self._insert_event(event)
            self._fault("after_event_append")
            cursor = self._db.execute(
                """
                UPDATE program_projections
                SET revision = ?, projection_json = ?, projection_digest = ?, last_sequence = ?
                WHERE program_id = ? AND revision = ?
                """,
                (
                    updated.revision,
                    record_to_json(updated),
                    canonical_digest(updated),
                    sequence,
                    program_id,
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise StaleProgramRevision(
                    f"Program revision changed during commit: {program_id}"
                )
            self._fault("after_projection_write")
        self._fault("after_commit")
        return updated

    def transition(
        self,
        program_id: str,
        target: ProgramStatus,
        *,
        expected_revision: int,
        event_id: str | None = None,
        occurred_at: str | None = None,
        recorded_at: str | None = None,
    ) -> Program:
        current = self.get(program_id)
        if current.revision != expected_revision:
            raise StaleProgramRevision(
                f"expected revision {expected_revision}, current revision {current.revision}"
            )
        if target is ProgramStatus.COMPLETED:
            raise InvalidRequest("Program completion is reserved for CompletionOracle")
        transition_program(current, target, expected_revision=expected_revision)
        event_type = _transition_event_type(current.status, target)
        return self._commit_change(
            program_id=program_id,
            expected_revision=expected_revision,
            event_type=event_type,
            mutate=lambda program: transition_program(
                program, target, expected_revision=expected_revision
            ),
            event_id=event_id,
            occurred_at=occurred_at,
            recorded_at=recorded_at,
        )

    def add_work(
        self,
        program_id: str,
        work_item: WorkItem,
        *,
        expected_revision: int,
        event_id: str | None = None,
        occurred_at: str | None = None,
        recorded_at: str | None = None,
    ) -> Program:
        return self._commit_change(
            program_id=program_id,
            expected_revision=expected_revision,
            event_type="program.work_added",
            mutate=lambda program: add_work_item(
                program, work_item, expected_revision=expected_revision
            ),
            event_id=event_id,
            occurred_at=occurred_at,
            recorded_at=recorded_at,
        )

    def satisfy_work(
        self,
        program_id: str,
        work_item_id: str,
        *,
        expected_revision: int,
        event_id: str | None = None,
        occurred_at: str | None = None,
        recorded_at: str | None = None,
    ) -> Program:
        return self._commit_change(
            program_id=program_id,
            expected_revision=expected_revision,
            event_type="program.work_satisfied",
            mutate=lambda program: satisfy_work_item(
                program, work_item_id, expected_revision=expected_revision
            ),
            event_id=event_id,
            occurred_at=occurred_at,
            recorded_at=recorded_at,
        )

    def list_events(self, program_id: str) -> tuple[Event, ...]:
        rows = self._db.execute(
            """
            SELECT sequence, event_id, program_id, event_type, event_json, event_digest
            FROM events WHERE program_id = ? ORDER BY sequence
            """,
            (program_id,),
        ).fetchall()
        events: list[Event] = []
        for row in rows:
            try:
                event = record_from_json(Event, row["event_json"])
            except (TypeError, ValueError) as exc:
                raise IntegrityViolation("Event cannot be decoded") from exc
            if not isinstance(event, Event):
                raise IntegrityViolation("decoded event is not an Event")
            if event.sequence != int(row["sequence"]):
                raise IntegrityViolation("Event row sequence disagrees with Event receipt")
            if event.event_id != row["event_id"]:
                raise IntegrityViolation("Event row identity disagrees with Event receipt")
            if event.program_id != row["program_id"]:
                raise IntegrityViolation("Event row Program disagrees with Event receipt")
            if event.event_type != row["event_type"]:
                raise IntegrityViolation("Event row type disagrees with Event receipt")
            if event.digest != row["event_digest"]:
                raise IntegrityViolation("Event row digest disagrees with Event receipt")
            events.append(event)
        return tuple(events)

    def rebuild(self, program_id: str) -> Program:
        events = self.list_events(program_id)
        if not events:
            raise InvalidRequest(f"no Event history for Program: {program_id}")
        previous_sequence = 0
        previous: Program | None = None
        rebuilt: Program | None = None
        for index, event in enumerate(events):
            if event.program_id != program_id:
                raise IntegrityViolation("Event Program identity mismatch")
            if event.sequence <= previous_sequence:
                raise IntegrityViolation("Event sequence is not strictly increasing")
            if not verify_event_digest(event):
                raise IntegrityViolation(f"Event digest mismatch: {event.event_id}")
            if index == 0 and event.event_type != "program.created":
                raise IntegrityViolation("Program Event history does not begin with creation")
            try:
                snapshot = event.payload["program"]
            except KeyError as exc:
                raise IntegrityViolation("Program Event lacks Program snapshot") from exc
            if not isinstance(snapshot, FrozenMap):
                raise IntegrityViolation("Program Event lacks canonical Program snapshot")
            try:
                candidate = record_from_json(Program, canonical_json(snapshot))
            except (TypeError, ValueError) as exc:
                raise IntegrityViolation("Program Event snapshot cannot be decoded") from exc
            if not isinstance(candidate, Program) or candidate.program_id != program_id:
                raise IntegrityViolation("Program Event snapshot identity mismatch")
            if previous is None:
                if candidate.revision != 0 or candidate.status is not ProgramStatus.CREATED:
                    raise IntegrityViolation(
                        "created Program snapshot must be revision 0 in created state"
                    )
            else:
                if candidate.revision != previous.revision + 1:
                    raise IntegrityViolation("Program Event revisions are not contiguous")
                _validate_event_semantics(previous, candidate, event.event_type)
            previous_sequence = event.sequence
            previous = candidate
            rebuilt = candidate
        assert rebuilt is not None
        return rebuilt

    def verify_integrity(self, program_id: str) -> None:
        row = self._read_projection_row(program_id)
        projected = self._program_from_row(row)
        rebuilt = self.rebuild(program_id)
        events = self.list_events(program_id)
        if canonical_json(projected) != canonical_json(rebuilt):
            raise IntegrityViolation("Program projection diverges from Event history")
        if int(row["last_sequence"]) != events[-1].sequence:
            raise IntegrityViolation("Program projection last_sequence is stale")

    def repair_projection(self, program_id: str) -> Program:
        rebuilt = self.rebuild(program_id)
        events = self.list_events(program_id)
        with self._transaction():
            self._db.execute(
                """
                INSERT INTO program_projections(
                    program_id, revision, projection_json, projection_digest, last_sequence
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(program_id) DO UPDATE SET
                    revision = excluded.revision,
                    projection_json = excluded.projection_json,
                    projection_digest = excluded.projection_digest,
                    last_sequence = excluded.last_sequence
                """,
                (
                    rebuilt.program_id,
                    rebuilt.revision,
                    record_to_json(rebuilt),
                    canonical_digest(rebuilt),
                    events[-1].sequence,
                ),
            )
        return rebuilt


def _transition_event_type(source: ProgramStatus, target: ProgramStatus) -> str:
    mapping = {
        (ProgramStatus.CREATED, ProgramStatus.ACTIVE): "program.activated",
        (ProgramStatus.CREATED, ProgramStatus.CANCELLED): "program.cancelled",
        (ProgramStatus.ACTIVE, ProgramStatus.BLOCKED): "program.blocked",
        (ProgramStatus.ACTIVE, ProgramStatus.COMPLETION_PENDING): "program.completion_proposed",
        (ProgramStatus.ACTIVE, ProgramStatus.FAILED): "program.failed",
        (ProgramStatus.ACTIVE, ProgramStatus.CANCELLED): "program.cancelled",
        (ProgramStatus.BLOCKED, ProgramStatus.ACTIVE): "program.unblocked",
        (ProgramStatus.BLOCKED, ProgramStatus.FAILED): "program.failed",
        (ProgramStatus.BLOCKED, ProgramStatus.CANCELLED): "program.cancelled",
        (ProgramStatus.COMPLETION_PENDING, ProgramStatus.ACTIVE): "program.completion_rejected",
        (ProgramStatus.COMPLETION_PENDING, ProgramStatus.BLOCKED): "program.completion_rejected",
        (ProgramStatus.COMPLETION_PENDING, ProgramStatus.COMPLETED): "program.completed",
        (ProgramStatus.COMPLETION_PENDING, ProgramStatus.FAILED): "program.failed",
        (ProgramStatus.COMPLETION_PENDING, ProgramStatus.CANCELLED): "program.cancelled",
    }
    try:
        return mapping[(source, target)]
    except KeyError as exc:
        raise InvalidRequest(
            f"no canonical Event type for transition {source.value} -> {target.value}"
        ) from exc


def _validate_event_semantics(
    previous: Program,
    candidate: Program,
    event_type: str,
) -> None:
    if candidate.status is not previous.status:
        expected = _transition_event_type(previous.status, candidate.status)
        if event_type != expected:
            raise IntegrityViolation(
                f"Event type {event_type} does not match Program transition {expected}"
            )
        if replace(
            candidate,
            revision=previous.revision,
            status=previous.status,
        ) != previous:
            raise IntegrityViolation("status Event changed non-status Program state")
        return

    if event_type == "program.work_added":
        if len(candidate.work_items) != len(previous.work_items) + 1:
            raise IntegrityViolation("work-added Event did not add exactly one WorkItem")
        if candidate.work_items[:-1] != previous.work_items:
            raise IntegrityViolation("work-added Event rewrote prior WorkItems")
        if candidate.work_items[-1].status is not WorkItemStatus.OPEN:
            raise IntegrityViolation("new WorkItem is not open")
        if replace(
            candidate,
            revision=previous.revision,
            work_items=previous.work_items,
        ) != previous:
            raise IntegrityViolation("work-added Event changed unrelated Program state")
        return

    if event_type == "program.work_satisfied":
        if len(candidate.work_items) != len(previous.work_items):
            raise IntegrityViolation("work-satisfied Event changed WorkItem count")
        changed = 0
        for before, after in zip(previous.work_items, candidate.work_items):
            if before == after:
                continue
            changed += 1
            if (
                before.work_item_id != after.work_item_id
                or before.description != after.description
                or before.status is not WorkItemStatus.OPEN
                or after.status is not WorkItemStatus.SATISFIED
            ):
                raise IntegrityViolation("work-satisfied Event made an invalid WorkItem change")
        if changed != 1:
            raise IntegrityViolation("work-satisfied Event must satisfy exactly one WorkItem")
        if replace(
            candidate,
            revision=previous.revision,
            work_items=previous.work_items,
        ) != previous:
            raise IntegrityViolation("work-satisfied Event changed unrelated Program state")
        return

    if event_type == "program.revised":
        if candidate.work_items != previous.work_items:
            raise IntegrityViolation("revised Event changed lifecycle-managed WorkItems")
        return

    raise IntegrityViolation(
        f"Event type {event_type} does not explain revision {candidate.revision}"
    )
