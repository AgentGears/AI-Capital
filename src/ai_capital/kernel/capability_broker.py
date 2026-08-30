from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from .capability_store import CapabilityRepository, capability_descriptor
from .errors import (
    CapabilityUnavailable,
    IntegrityViolation,
    InvalidRequest,
    StaleCapabilityBinding,
)
from .frozen_json import FrozenMap
from .models import (
    CapabilityDescriptor,
    CapabilityRequest,
    CapabilityResolution,
    CapabilitySnapshot,
    ResolvedEffect,
)
from .structured_schema import validate_structured_value


class CapabilityEffectResolver(Protocol):
    """K3 handler seam: resolve semantic effect only; never execute it."""

    def resolve_effect(self, arguments: FrozenMap) -> ResolvedEffect: ...


class CapabilityHandlerRegistry:
    """Runtime bindings for effect resolvers; presence implies availability only."""

    def __init__(self):
        self._handlers: dict[str, CapabilityEffectResolver] = {}

    def register(self, binding_id: str, handler: CapabilityEffectResolver) -> None:
        if not binding_id.strip():
            raise InvalidRequest("Capability handler binding ID must be non-empty")
        if binding_id in self._handlers:
            raise InvalidRequest(f"Capability handler binding already registered: {binding_id}")
        self._handlers[binding_id] = handler

    def contains(self, binding_id: str) -> bool:
        return binding_id in self._handlers

    def resolve(self, binding_id: str) -> CapabilityEffectResolver:
        try:
            return self._handlers[binding_id]
        except KeyError as exc:
            raise CapabilityUnavailable(
                f"Capability handler binding is unavailable: {binding_id}"
            ) from exc


class CapabilityBroker:
    """Validates requests and resolves effects without granting or executing authority."""

    def __init__(
        self,
        capabilities: CapabilityRepository,
        handlers: CapabilityHandlerRegistry,
    ):
        self._capabilities = capabilities
        self._handlers = handlers

    def available_descriptors(self) -> tuple[CapabilityDescriptor, ...]:
        descriptors = [
            capability_descriptor(capability)
            for capability in self._capabilities.all()
            if self._handlers.contains(capability.handler_binding)
        ]
        return tuple(sorted(descriptors, key=lambda item: item.capability_id))

    def snapshot(
        self,
        capability_ids: Iterable[str] | None = None,
    ) -> CapabilitySnapshot:
        if capability_ids is None:
            descriptors = self.available_descriptors()
        else:
            descriptors_list: list[CapabilityDescriptor] = []
            seen: set[str] = set()
            for capability_id in capability_ids:
                if capability_id in seen:
                    raise InvalidRequest(
                        f"duplicate Capability requested for snapshot: {capability_id}"
                    )
                seen.add(capability_id)
                capability = self._capabilities.get(capability_id)
                if not self._handlers.contains(capability.handler_binding):
                    raise CapabilityUnavailable(capability_id)
                descriptors_list.append(capability_descriptor(capability))
            descriptors = tuple(descriptors_list)
        return self._capabilities.create_snapshot(descriptors)

    def resolve(
        self,
        request: CapabilityRequest,
        *,
        snapshot: CapabilitySnapshot,
    ) -> CapabilityResolution:
        durable_snapshot = self._capabilities.get_snapshot(snapshot.snapshot_id)
        if durable_snapshot != snapshot:
            raise IntegrityViolation("supplied Capability snapshot differs from durable snapshot")
        descriptor = _descriptor_from_snapshot(durable_snapshot, request.capability_id)
        current = self._capabilities.get(request.capability_id)

        if (
            request.expected_binding_revision != descriptor.binding_revision
            or current.binding_revision != descriptor.binding_revision
        ):
            raise StaleCapabilityBinding(
                f"Capability request binding {request.expected_binding_revision} does not "
                f"match snapshot/current binding {descriptor.binding_revision}/"
                f"{current.binding_revision}"
            )

        if capability_descriptor(current) != descriptor:
            raise IntegrityViolation("Capability snapshot descriptor disagrees with current contract")

        arguments = validate_structured_value(
            descriptor.input_schema,
            request.arguments,
            path="$arguments",
        )
        if not isinstance(arguments, FrozenMap):
            raise IntegrityViolation("Capability arguments did not normalize to an object")

        resolver = self._handlers.resolve(current.handler_binding)
        try:
            effect = resolver.resolve_effect(arguments)
        except Exception as exc:
            raise IntegrityViolation("Capability effect resolver failed") from exc
        if not isinstance(effect, ResolvedEffect):
            raise IntegrityViolation("Capability effect resolver returned invalid type")
        if effect.resource_type != current.resource_type:
            raise IntegrityViolation("resolved effect resource type violates Capability contract")
        if effect.effect_class is not current.effect_class:
            raise IntegrityViolation("resolved effect class violates Capability contract")
        if not effect.target.strip():
            raise IntegrityViolation("resolved effect target must be non-empty")

        return CapabilityResolution(
            request_id=request.request_id,
            capability_id=current.capability_id,
            binding_revision=current.binding_revision,
            arguments=arguments,
            resolved_effect=effect,
        )


def _descriptor_from_snapshot(
    snapshot: CapabilitySnapshot,
    capability_id: str,
) -> CapabilityDescriptor:
    for descriptor in snapshot.capabilities:
        if descriptor.capability_id == capability_id:
            return descriptor
    raise CapabilityUnavailable(
        f"Capability was not exposed in snapshot {snapshot.snapshot_id}: {capability_id}"
    )
