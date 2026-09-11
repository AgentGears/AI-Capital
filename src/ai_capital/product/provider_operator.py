from __future__ import annotations

from pathlib import Path

from ..kernel.durable_program import ProgramRepository


class LocalProviderOperator:
    """Local product surface for durable provider configuration."""

    def __init__(self, programs: ProgramRepository, *, owns_repository: bool = False):
        self._programs = programs
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
