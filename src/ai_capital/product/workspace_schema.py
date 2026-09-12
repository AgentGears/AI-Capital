from __future__ import annotations

from ..kernel.durable_program import ProgramRepository
from ..kernel.errors import IntegrityViolation


COMPONENT = "product_workspace_archive"
COMPONENT_SCHEMA_VERSION = 1
TABLES = (
    "workspace_artifacts",
    "workspace_snapshots",
    "workspace_snapshot_entries",
    "program_bundle_imports",
)
_EXPECTED_COLUMNS = {
    "workspace_artifacts": (
        "artifact_digest", "content_ref", "byte_length",
    ),
    "workspace_snapshots": (
        "snapshot_id", "program_id", "program_revision", "manifest_digest",
        "snapshot_json", "snapshot_digest",
    ),
    "workspace_snapshot_entries": (
        "snapshot_id", "path", "artifact_digest", "entry_json", "entry_digest",
    ),
    "program_bundle_imports": (
        "bundle_id", "source_program_id", "bundle_json", "bundle_digest", "imported_at",
    ),
}
_EXPECTED_PRIMARY = {
    "workspace_artifacts": (("artifact_digest", 1),),
    "workspace_snapshots": (("snapshot_id", 1),),
    "workspace_snapshot_entries": (("snapshot_id", 1), ("path", 2)),
    "program_bundle_imports": (("bundle_id", 1),),
}
_EXPECTED_UNIQUE = {
    "workspace_artifacts": {("artifact_digest",), ("content_ref",)},
    "workspace_snapshots": {("snapshot_id",)},
    "workspace_snapshot_entries": {("snapshot_id", "path")},
    "program_bundle_imports": {("bundle_id",)},
}


def _table_exists(programs: ProgramRepository, table: str) -> bool:
    row = programs._db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _unique_keys(programs: ProgramRepository, table: str) -> set[tuple[str, ...]]:
    result: set[tuple[str, ...]] = set()
    for index in programs._db.execute(f"PRAGMA index_list({table})").fetchall():
        if type(index["unique"]) is not int or index["unique"] != 1:
            continue
        columns = tuple(
            str(row["name"])
            for row in programs._db.execute(
                f"PRAGMA index_info({index['name']})"
            ).fetchall()
        )
        if columns:
            result.add(columns)
    return result


def verify_schema(programs: ProgramRepository) -> None:
    for table, expected_columns in _EXPECTED_COLUMNS.items():
        if not _table_exists(programs, table):
            raise IntegrityViolation(f"workspace archive schema is missing {table}")
        info = programs._db.execute(f"PRAGMA table_info({table})").fetchall()
        columns = tuple(str(row["name"]) for row in info)
        if columns != expected_columns:
            raise IntegrityViolation(f"workspace archive schema shape mismatch: {table}")
        primary = tuple(
            (str(row["name"]), int(row["pk"]))
            for row in info
            if type(row["pk"]) is int and row["pk"] != 0
        )
        if primary != _EXPECTED_PRIMARY[table]:
            raise IntegrityViolation(f"workspace archive primary key mismatch: {table}")
        not_null = {
            str(row["name"])
            for row in info
            if type(row["notnull"]) is int and row["notnull"] == 1
        }
        required = set(expected_columns) - {expected_columns[0]}
        if not required.issubset(not_null):
            raise IntegrityViolation(f"workspace archive nullability mismatch: {table}")
        if not _EXPECTED_UNIQUE[table].issubset(_unique_keys(programs, table)):
            raise IntegrityViolation(f"workspace archive unique-key mismatch: {table}")


def _install_guards(programs: ProgramRepository) -> None:
    for table in TABLES:
        for operation in ("UPDATE", "DELETE"):
            trigger = f"{table}_immutable_{operation.lower()}"
            programs._db.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS {trigger}
                BEFORE {operation} ON {table}
                BEGIN
                    SELECT RAISE(ABORT, 'workspace archive records are immutable');
                END
                """
            )


def _create_schema(programs: ProgramRepository) -> None:
    programs._db.execute(
        """
        CREATE TABLE workspace_artifacts (
            artifact_digest TEXT PRIMARY KEY,
            content_ref TEXT NOT NULL UNIQUE,
            byte_length INTEGER NOT NULL
        )
        """
    )
    programs._db.execute(
        """
        CREATE TABLE workspace_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            program_id TEXT NOT NULL,
            program_revision INTEGER NOT NULL,
            manifest_digest TEXT NOT NULL,
            snapshot_json TEXT NOT NULL,
            snapshot_digest TEXT NOT NULL
        )
        """
    )
    programs._db.execute(
        """
        CREATE TABLE workspace_snapshot_entries (
            snapshot_id TEXT NOT NULL,
            path TEXT NOT NULL,
            artifact_digest TEXT NOT NULL,
            entry_json TEXT NOT NULL,
            entry_digest TEXT NOT NULL,
            PRIMARY KEY(snapshot_id, path),
            FOREIGN KEY(snapshot_id) REFERENCES workspace_snapshots(snapshot_id),
            FOREIGN KEY(artifact_digest) REFERENCES workspace_artifacts(artifact_digest)
        )
        """
    )
    programs._db.execute(
        """
        CREATE TABLE program_bundle_imports (
            bundle_id TEXT PRIMARY KEY,
            source_program_id TEXT NOT NULL,
            bundle_json TEXT NOT NULL,
            bundle_digest TEXT NOT NULL,
            imported_at TEXT NOT NULL
        )
        """
    )


def migrate_workspace_archive(programs: ProgramRepository) -> None:
    with programs._transaction():
        programs._db.execute(
            """
            CREATE TABLE IF NOT EXISTS component_schema (
                component TEXT PRIMARY KEY,
                version INTEGER NOT NULL
            )
            """
        )
        row = programs._db.execute(
            "SELECT version FROM component_schema WHERE component = ?", (COMPONENT,)
        ).fetchone()
        if row is not None:
            version = row["version"]
            if type(version) is not int:
                raise IntegrityViolation("workspace archive schema version is malformed")
            if version != COMPONENT_SCHEMA_VERSION:
                raise IntegrityViolation(
                    f"unsupported workspace archive schema version {version}"
                )
            verify_schema(programs)
            _install_guards(programs)
            return
        if any(_table_exists(programs, table) for table in TABLES):
            raise IntegrityViolation("workspace archive tables exist without a schema marker")
        _create_schema(programs)
        verify_schema(programs)
        _install_guards(programs)
        programs._db.execute(
            "INSERT INTO component_schema(component, version) VALUES (?, ?)",
            (COMPONENT, COMPONENT_SCHEMA_VERSION),
        )
