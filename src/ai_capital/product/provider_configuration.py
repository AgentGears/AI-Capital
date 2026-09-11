from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from typing import Mapping

from ..kernel.durable_program import ProgramRepository
from ..kernel.errors import (
    IntegrityViolation,
    InvalidRequest,
    PersistenceConflict,
    StaleProviderConfigurationRevision,
)
from ..kernel.events import utc_now
from ..kernel.frozen_json import FrozenMap, freeze_json
from ..kernel.schema_codec import record_from_json, record_to_json
from ..kernel.serialization import canonical_digest


_COMPONENT = "product_provider_configuration"
_COMPONENT_SCHEMA_VERSION = 1
_HISTORY_TABLE = "provider_configuration_revisions"
_PROJECTION_TABLE = "provider_configuration_projections"
_ALLOWED_SETTINGS = frozenset(
    {
        "temperature",
        "top_p",
        "max_output_units",
        "timeout_seconds",
        "seed",
        "reasoning_effort",
        "response_format",
    }
)
_EXPECTED_COLUMNS = {
    _HISTORY_TABLE: (
        "binding_id",
        "revision",
        "configuration_json",
        "configuration_digest",
    ),
    _PROJECTION_TABLE: (
        "binding_id",
        "revision",
        "configuration_json",
        "configuration_digest",
    ),
}


@dataclass(frozen=True, slots=True)
class ProviderConfiguration:
    """Non-secret product metadata for one runtime model-binding identity."""

    binding_id: str
    revision: int
    adapter: str
    model: str
    settings: FrozenMap
    configured_at: str

    def __post_init__(self) -> None:
        frozen = freeze_json(self.settings)
        if not isinstance(frozen, FrozenMap):
            raise TypeError("provider settings must be an object")
        object.__setattr__(self, "settings", frozen)


def _require_text(value: str, *, field: str) -> str:
    if type(value) is not str or not value.strip():
        raise InvalidRequest(f"{field} must be non-empty")
    return value


def _number(value: object, *, field: str) -> float:
    if type(value) not in {int, float}:
        raise InvalidRequest(f"provider setting {field} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise InvalidRequest(f"provider setting {field} must be finite")
    return result


def _validate_settings(settings: FrozenMap) -> None:
    unknown = sorted(set(settings) - _ALLOWED_SETTINGS)
    if unknown:
        raise InvalidRequest(
            "provider settings contain unsupported or secret-capable fields: "
            + ", ".join(unknown)
        )
    if "temperature" in settings:
        value = _number(settings["temperature"], field="temperature")
        if value < 0 or value > 2:
            raise InvalidRequest("provider setting temperature must be between 0 and 2")
    if "top_p" in settings:
        value = _number(settings["top_p"], field="top_p")
        if value < 0 or value > 1:
            raise InvalidRequest("provider setting top_p must be between 0 and 1")
    if "max_output_units" in settings:
        value = settings["max_output_units"]
        if type(value) is not int or value <= 0:
            raise InvalidRequest("provider setting max_output_units must be a positive integer")
    if "timeout_seconds" in settings:
        value = _number(settings["timeout_seconds"], field="timeout_seconds")
        if value <= 0:
            raise InvalidRequest("provider setting timeout_seconds must be positive")
    if "seed" in settings and type(settings["seed"]) is not int:
        raise InvalidRequest("provider setting seed must be an integer")
    if "reasoning_effort" in settings:
        if settings["reasoning_effort"] not in {"low", "medium", "high"}:
            raise InvalidRequest(
                "provider setting reasoning_effort must be low, medium, or high"
            )
    if "response_format" in settings:
        if settings["response_format"] not in {"text", "json"}:
            raise InvalidRequest("provider setting response_format must be text or json")


def validate_provider_configuration(configuration: ProviderConfiguration) -> None:
    _require_text(configuration.binding_id, field="binding_id")
    _require_text(configuration.adapter, field="adapter")
    _require_text(configuration.model, field="model")
    if type(configuration.revision) is not int or configuration.revision < 0:
        raise InvalidRequest("provider configuration revision must be non-negative")
    _require_text(configuration.configured_at, field="configured_at")
    _validate_settings(configuration.settings)


class ProviderConfigurationRepository:
    """Durable product-owned provider metadata; never an Authority source."""

    def __init__(self, host_store: ProgramRepository):
        self._host_store = host_store
        self._migrate()

    def _table_exists(self, table_name: str) -> bool:
        row = self._host_store._db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        return row is not None

    def _verify_schema_shape(self) -> None:
        for table_name, expected_columns in _EXPECTED_COLUMNS.items():
            if not self._table_exists(table_name):
                raise IntegrityViolation(
                    f"provider configuration schema is missing {table_name}"
                )
            columns = tuple(
                str(row["name"])
                for row in self._host_store._db.execute(
                    f"PRAGMA table_info({table_name})"
                ).fetchall()
            )
            if columns != expected_columns:
                raise IntegrityViolation(
                    f"provider configuration schema shape mismatch: {table_name}"
                )

    def _install_history_guards(self) -> None:
        self._host_store._db.execute(
            """
            CREATE TRIGGER IF NOT EXISTS provider_configuration_history_no_update
            BEFORE UPDATE ON provider_configuration_revisions
            BEGIN
                SELECT RAISE(ABORT, 'provider configuration history is immutable');
            END
            """
        )
        self._host_store._db.execute(
            """
            CREATE TRIGGER IF NOT EXISTS provider_configuration_history_no_delete
            BEFORE DELETE ON provider_configuration_revisions
            BEGIN
                SELECT RAISE(ABORT, 'provider configuration history is immutable');
            END
            """
        )

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
                try:
                    version = int(row["version"])
                except (TypeError, ValueError) as exc:
                    raise IntegrityViolation(
                        "provider configuration schema version is malformed"
                    ) from exc
                if version > _COMPONENT_SCHEMA_VERSION:
                    raise IntegrityViolation(
                        f"provider configuration schema version {version} is newer than supported "
                        f"{_COMPONENT_SCHEMA_VERSION}"
                    )
                if version != _COMPONENT_SCHEMA_VERSION:
                    raise IntegrityViolation(
                        f"unsupported provider configuration schema version {version}"
                    )
                self._verify_schema_shape()
                self._install_history_guards()
                return

            if any(self._table_exists(table_name) for table_name in _EXPECTED_COLUMNS):
                raise IntegrityViolation(
                    "provider configuration tables exist without a schema marker"
                )
            self._host_store._db.execute(
                """
                CREATE TABLE provider_configuration_revisions (
                    binding_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    configuration_json TEXT NOT NULL,
                    configuration_digest TEXT NOT NULL,
                    PRIMARY KEY(binding_id, revision)
                )
                """
            )
            self._host_store._db.execute(
                """
                CREATE TABLE provider_configuration_projections (
                    binding_id TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL,
                    configuration_json TEXT NOT NULL,
                    configuration_digest TEXT NOT NULL
                )
                """
            )
            self._verify_schema_shape()
            self._install_history_guards()
            self._host_store._db.execute(
                "INSERT INTO component_schema(component, version) VALUES (?, ?)",
                (_COMPONENT, _COMPONENT_SCHEMA_VERSION),
            )

    @staticmethod
    def _configuration_from_row(row: sqlite3.Row) -> ProviderConfiguration:
        try:
            configuration = record_from_json(
                ProviderConfiguration,
                row["configuration_json"],
            )
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation("provider configuration cannot be decoded") from exc
        if not isinstance(configuration, ProviderConfiguration):
            raise IntegrityViolation("provider configuration decoded wrong type")
        try:
            validate_provider_configuration(configuration)
        except InvalidRequest as exc:
            raise IntegrityViolation("provider configuration is invalid") from exc
        if (
            configuration.binding_id != row["binding_id"]
            or configuration.revision != int(row["revision"])
            or canonical_digest(configuration) != row["configuration_digest"]
        ):
            raise IntegrityViolation("provider configuration row/digest binding mismatch")
        return configuration

    def _projection_row(self, binding_id: str) -> sqlite3.Row:
        row = self._host_store._db.execute(
            """
            SELECT binding_id, revision, configuration_json, configuration_digest
            FROM provider_configuration_projections WHERE binding_id = ?
            """,
            (binding_id,),
        ).fetchone()
        if row is None:
            raise InvalidRequest(f"unknown provider binding: {binding_id}")
        return row

    def _validated_history(self, binding_id: str) -> tuple[ProviderConfiguration, ...]:
        projection_row = self._projection_row(binding_id)
        projection = self._configuration_from_row(projection_row)
        rows = self._host_store._db.execute(
            """
            SELECT binding_id, revision, configuration_json, configuration_digest
            FROM provider_configuration_revisions
            WHERE binding_id = ? ORDER BY revision
            """,
            (binding_id,),
        ).fetchall()
        if not rows:
            raise IntegrityViolation("provider configuration projection lacks history")
        history = tuple(self._configuration_from_row(row) for row in rows)
        for expected_revision, configuration in enumerate(history):
            if configuration.revision != expected_revision:
                raise IntegrityViolation(
                    "provider configuration revision history is not contiguous"
                )
        if history[-1] != projection:
            raise IntegrityViolation(
                "provider configuration projection diverges from revision history"
            )
        return history

    @staticmethod
    def _new_configuration(
        *,
        binding_id: str,
        revision: int,
        adapter: str,
        model: str,
        settings: Mapping[str, object],
    ) -> ProviderConfiguration:
        try:
            frozen = freeze_json(settings)
        except (TypeError, ValueError) as exc:
            raise InvalidRequest("provider settings must be canonical JSON") from exc
        if not isinstance(frozen, FrozenMap):
            raise InvalidRequest("provider settings must be an object")
        configuration = ProviderConfiguration(
            binding_id=binding_id,
            revision=revision,
            adapter=adapter,
            model=model,
            settings=frozen,
            configured_at=utc_now(),
        )
        validate_provider_configuration(configuration)
        return configuration

    def register(
        self,
        *,
        binding_id: str,
        adapter: str,
        model: str,
        settings: Mapping[str, object] | None = None,
    ) -> ProviderConfiguration:
        configuration = self._new_configuration(
            binding_id=binding_id,
            revision=0,
            adapter=adapter,
            model=model,
            settings={} if settings is None else settings,
        )
        encoded = record_to_json(configuration)
        digest = canonical_digest(configuration)
        try:
            with self._host_store._transaction():
                history = self._host_store._db.execute(
                    "SELECT 1 FROM provider_configuration_revisions WHERE binding_id = ? LIMIT 1",
                    (binding_id,),
                ).fetchone()
                projection = self._host_store._db.execute(
                    "SELECT 1 FROM provider_configuration_projections WHERE binding_id = ?",
                    (binding_id,),
                ).fetchone()
                if history is not None or projection is not None:
                    raise PersistenceConflict(
                        f"provider binding already exists: {binding_id}"
                    )
                self._host_store._db.execute(
                    """
                    INSERT INTO provider_configuration_revisions(
                        binding_id, revision, configuration_json, configuration_digest
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (binding_id, 0, encoded, digest),
                )
                self._host_store._db.execute(
                    """
                    INSERT INTO provider_configuration_projections(
                        binding_id, revision, configuration_json, configuration_digest
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (binding_id, 0, encoded, digest),
                )
        except sqlite3.IntegrityError as exc:
            raise PersistenceConflict(
                f"provider binding already exists: {binding_id}"
            ) from exc
        return configuration

    def get(self, binding_id: str) -> ProviderConfiguration:
        _require_text(binding_id, field="binding_id")
        return self._validated_history(binding_id)[-1]

    def list(self) -> tuple[ProviderConfiguration, ...]:
        rows = self._host_store._db.execute(
            "SELECT binding_id FROM provider_configuration_projections ORDER BY binding_id"
        ).fetchall()
        return tuple(self.get(str(row["binding_id"])) for row in rows)

    def history(self, binding_id: str) -> tuple[ProviderConfiguration, ...]:
        _require_text(binding_id, field="binding_id")
        return self._validated_history(binding_id)

    def update(
        self,
        binding_id: str,
        *,
        expected_revision: int,
        adapter: str,
        model: str,
        settings: Mapping[str, object] | None = None,
    ) -> ProviderConfiguration:
        _require_text(binding_id, field="binding_id")
        if type(expected_revision) is not int or expected_revision < 0:
            raise InvalidRequest("expected provider revision must be non-negative")
        with self._host_store._transaction():
            current = self._validated_history(binding_id)[-1]
            if current.revision != expected_revision:
                raise StaleProviderConfigurationRevision(
                    f"expected provider revision {expected_revision}, current revision {current.revision}"
                )
            updated = self._new_configuration(
                binding_id=binding_id,
                revision=current.revision + 1,
                adapter=adapter,
                model=model,
                settings={} if settings is None else settings,
            )
            encoded = record_to_json(updated)
            digest = canonical_digest(updated)
            self._host_store._db.execute(
                """
                INSERT INTO provider_configuration_revisions(
                    binding_id, revision, configuration_json, configuration_digest
                ) VALUES (?, ?, ?, ?)
                """,
                (binding_id, updated.revision, encoded, digest),
            )
            cursor = self._host_store._db.execute(
                """
                UPDATE provider_configuration_projections
                SET revision = ?, configuration_json = ?, configuration_digest = ?
                WHERE binding_id = ? AND revision = ?
                """,
                (
                    updated.revision,
                    encoded,
                    digest,
                    binding_id,
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise StaleProviderConfigurationRevision(
                    f"provider revision changed during update: {binding_id}"
                )
        return updated
