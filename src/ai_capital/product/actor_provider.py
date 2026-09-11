from __future__ import annotations

from pathlib import Path
from typing import Any

from ..kernel.actor_store import ActorRepository
from ..kernel.durable_program import ProgramRepository
from ..kernel.errors import InvalidRequest, StaleProviderConfigurationRevision
from ..kernel.serialization import to_canonical_data
from .provider_configuration import ProviderConfigurationRepository


class LocalActorProviderOperator:
    """Inspect and replace an Actor model binding through existing Actor authority."""

    def __init__(self, programs: ProgramRepository, *, owns_repository: bool = False):
        self._programs = programs
        self._actors: ActorRepository | None = None
        self._providers: ProviderConfigurationRepository | None = None
        self._owns_repository = owns_repository
        self._closed = False

    @classmethod
    def open(cls, database_path: str | Path) -> "LocalActorProviderOperator":
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

    def __enter__(self) -> "LocalActorProviderOperator":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise InvalidRequest("local Actor provider operator is closed")

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
        return None if row is None else int(row["version"])

    def _actor_repository(self) -> ActorRepository:
        self._ensure_open()
        if self._component_version("actor_inference") is None:
            raise InvalidRequest("Actor store is not initialized")
        if self._actors is None:
            self._actors = ActorRepository(self._programs)
        return self._actors

    def _provider_repository(self, *, required: bool) -> ProviderConfigurationRepository | None:
        self._ensure_open()
        if self._component_version("product_provider_configuration") is None:
            if required:
                raise InvalidRequest("provider configuration is not initialized")
            return None
        if self._providers is None:
            self._providers = ProviderConfigurationRepository(self._programs)
        return self._providers

    def show(self, actor_id: str) -> dict[str, Any]:
        actor_id = self._require_text(actor_id, field="actor_id")
        actor = self._actor_repository().get(actor_id)
        provider = None
        repository = self._provider_repository(required=False)
        if repository is not None:
            row = self._programs._db.execute(
                """
                SELECT 1 FROM provider_configuration_projections
                WHERE binding_id = ?
                """,
                (actor.model_binding,),
            ).fetchone()
            if row is not None:
                provider = to_canonical_data(repository.get(actor.model_binding))
        return {
            "actor": to_canonical_data(actor),
            "provider": provider,
            "configured": provider is not None,
        }

    def rebind(
        self,
        actor_id: str,
        binding_id: str,
        *,
        expected_generation: int,
        expected_provider_revision: int,
    ) -> dict[str, Any]:
        actor_id = self._require_text(actor_id, field="actor_id")
        binding_id = self._require_text(binding_id, field="binding_id")
        repository = self._provider_repository(required=True)
        assert repository is not None
        configuration = repository.get(binding_id)
        if configuration.revision != expected_provider_revision:
            raise StaleProviderConfigurationRevision(
                f"expected provider revision {expected_provider_revision}, "
                f"current revision {configuration.revision}"
            )
        actors = self._actor_repository()
        actors.replace_binding(
            actor_id,
            binding_id,
            expected_generation=expected_generation,
        )
        return self.show(actor_id)
