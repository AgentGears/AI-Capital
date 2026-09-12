from __future__ import annotations

from pathlib import Path
from typing import Any

from ..kernel.durable_program import ProgramRepository
from ..kernel.errors import InvalidRequest
from .program_bundle_store import ProgramBundleStore
from .workspace_snapshots import WorkspaceSnapshotStore


class LocalWorkspaceOperator:
    """Local product surface for snapshots, artifacts, and read-only Program archives."""

    def __init__(
        self,
        programs: ProgramRepository,
        *,
        workspace_root: str | Path | None = None,
        artifact_root: str | Path | None = None,
        owns_repository: bool = False,
    ):
        database_path = str(programs._database_path)
        if workspace_root is None or artifact_root is None:
            if database_path == ":memory:":
                raise InvalidRequest(
                    "in-memory workspace operators require explicit workspace and artifact roots"
                )
            parent = Path(database_path).resolve().parent
            if workspace_root is None:
                workspace_root = parent / "workspace"
            if artifact_root is None:
                artifact_root = parent / "artifacts"
        self._programs = programs
        self._snapshots = WorkspaceSnapshotStore(
            programs,
            workspace_root=workspace_root,
            artifact_root=artifact_root,
        )
        self._bundles = ProgramBundleStore(programs, self._snapshots)
        self._owns_repository = owns_repository
        self._closed = False

    @classmethod
    def open(
        cls,
        database_path: str | Path,
        *,
        workspace_root: str | Path | None = None,
        artifact_root: str | Path | None = None,
    ) -> "LocalWorkspaceOperator":
        programs = ProgramRepository(database_path)
        try:
            return cls(
                programs,
                workspace_root=workspace_root,
                artifact_root=artifact_root,
                owns_repository=True,
            )
        except Exception:
            programs.close()
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_repository:
            self._programs.close()

    def __enter__(self) -> "LocalWorkspaceOperator":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise InvalidRequest("local workspace operator is closed")

    def snapshot(self, program_id: str) -> dict[str, Any]:
        self._ensure_open()
        return self._snapshots.capture(program_id)

    def snapshots(self, program_id: str) -> tuple[dict[str, Any], ...]:
        self._ensure_open()
        return self._snapshots.list(program_id)

    def show_snapshot(self, snapshot_id: str) -> dict[str, Any]:
        self._ensure_open()
        return self._snapshots.show(snapshot_id)

    def artifacts(self, snapshot_id: str) -> tuple[dict[str, Any], ...]:
        self._ensure_open()
        return self._snapshots.artifacts(snapshot_id)

    def artifact_bytes(self, snapshot_id: str, path: str) -> bytes:
        self._ensure_open()
        return self._snapshots.artifact_bytes(snapshot_id, path)

    def export_bundle(self, program_id: str, snapshot_id: str) -> bytes:
        self._ensure_open()
        return self._bundles.export(program_id, snapshot_id)

    def import_bundle(self, content: bytes) -> dict[str, Any]:
        self._ensure_open()
        return self._bundles.import_archive(content)

    def bundles(self) -> tuple[dict[str, Any], ...]:
        self._ensure_open()
        return self._bundles.list()

    def show_bundle(self, bundle_id: str) -> dict[str, Any]:
        self._ensure_open()
        return self._bundles.show(bundle_id)

    def bundle_artifacts(self, bundle_id: str) -> tuple[dict[str, Any], ...]:
        self._ensure_open()
        return self._bundles.artifacts(bundle_id)

    def bundle_artifact_bytes(self, bundle_id: str, path: str) -> bytes:
        self._ensure_open()
        return self._bundles.artifact_bytes(bundle_id, path)
