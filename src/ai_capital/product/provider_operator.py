from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from ..kernel.durable_program import ProgramRepository
from ..kernel.errors import IntegrityViolation, InvalidRequest
from ..kernel.serialization import to_canonical_data
from .provider_configuration import ProviderConfigurationRepository


_PROVIDER_TABLES = (
    "provider_configuration_revisions",
    "provider_configuration_projections",
)


class LocalProviderOperator:
    """Local product surface for durable non-secret provider configuration."""

    def __init__(self, programs: ProgramRepository, *, owns_repository: bool = False):
        self._programs = programs
        self._providers: ProviderConfigurationRepository | None = None
        self._owns_repository = owns_repository
        self._closed = False

    @classmethod
    def open(cls, database_path: str | Path) -> "LocalProviderOperator":
        programs = ProgramRepository(database_path)
        try:
            return cls(programs, owns_repository=True)
        except Exception:
            programs.close()
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_repository:
            self._programs.close()

    def __enter__(self) -> "LocalProviderOperator":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise InvalidRequest("local provider operator is closed")

    @staticmethod
    def _require_text(value: str, *, field: str) -> str:
        if type(value) is not str or not value.strip():
            raise InvalidRequest(f"{field} must be non-empty")
        return value

    def _component_version(self, component: str) -> int | None:
        table = self._programs._db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'component_schema'"
        ).fetchone()
        if table is None:
            return None
        row = self._programs._db.execute(
            "SELECT version FROM component_schema WHERE component = ?",
            (component,),
        ).fetchone()
        if row is None:
            return None
        try:
            return int(row["version"])
        except (TypeError, ValueError) as exc:
            raise IntegrityViolation(f"{component} schema version is malformed") from exc

    def _provider_tables_exist(self) -> bool:
        placeholders = ",".join("?" for _ in _PROVIDER_TABLES)
        row = self._programs._db.execute(
            f"""
            SELECT COUNT(*) AS count FROM sqlite_master
            WHERE type = 'table' AND name IN ({placeholders})
            """,
            _PROVIDER_TABLES,
        ).fetchone()
        return int(row["count"]) != 0

    def _repository(
        self,
        *,
        required: bool = False,
        initialize: bool = False,
    ) -> ProviderConfigurationRepository | None:
        self._ensure_open()
        version = self._component_version("product_provider_configuration")
        if version is None and not initialize:
            if self._provider_tables_exist():
                # Repository admission fails closed on orphan backing tables.
                return ProviderConfigurationRepository(self._programs)
            if required:
                raise InvalidRequest("provider configuration is not initialized")
            return None
        if self._providers is None:
            self._providers = ProviderConfigurationRepository(self._programs)
        return self._providers

    def register(
        self,
        *,
        binding_id: str,
        adapter: str,
        model: str,
        settings: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        repository = self._repository(initialize=True)
        assert repository is not None
        return to_canonical_data(
            repository.register(
                binding_id=binding_id,
                adapter=adapter,
                model=model,
                settings=settings,
            )
        )

    def update(
        self,
        binding_id: str,
        *,
        expected_revision: int,
        adapter: str,
        model: str,
        settings: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        binding_id = self._require_text(binding_id, field="binding_id")
        repository = self._repository(required=True)
        assert repository is not None
        return to_canonical_data(
            repository.update(
                binding_id,
                expected_revision=expected_revision,
                adapter=adapter,
                model=model,
                settings=settings,
            )
        )

    def list(self) -> tuple[dict[str, Any], ...]:
        repository = self._repository(required=False)
        if repository is None:
            return ()
        return tuple(to_canonical_data(item) for item in repository.list())

    def show(self, binding_id: str) -> dict[str, Any]:
        binding_id = self._require_text(binding_id, field="binding_id")
        repository = self._repository(required=True)
        assert repository is not None
        return to_canonical_data(repository.get(binding_id))

    def history(self, binding_id: str) -> tuple[dict[str, Any], ...]:
        binding_id = self._require_text(binding_id, field="binding_id")
        repository = self._repository(required=True)
        assert repository is not None
        return tuple(to_canonical_data(item) for item in repository.history(binding_id))
