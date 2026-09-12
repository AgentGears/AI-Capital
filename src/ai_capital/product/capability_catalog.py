from __future__ import annotations

from dataclasses import dataclass

from ..kernel.builtin_capabilities import install_builtin_capabilities
from ..kernel.capability_broker import CapabilityHandlerRegistry
from ..kernel.capability_store import CapabilityRepository
from ..kernel.enums import EffectClass, Reversibility, RiskClass
from ..kernel.errors import IntegrityViolation, UnknownCapability
from ..kernel.frozen_json import FrozenMap
from ..kernel.models import Capability, ResolvedEffect


def _object_schema(
    properties: dict[str, object],
    required: tuple[str, ...],
) -> dict[str, object]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additional_properties": False,
    }


def _string_schema(*, enum: tuple[str, ...] | None = None) -> dict[str, object]:
    result: dict[str, object] = {"type": "string", "min_length": 1}
    if enum is not None:
        result["enum"] = enum
    return result


def _open_output_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {},
        "required": (),
        "additional_properties": True,
    }


@dataclass(frozen=True, slots=True)
class TargetResolver:
    resource_type: str
    effect_class: EffectClass
    target_field: str

    def resolve_effect(self, arguments: FrozenMap) -> ResolvedEffect:
        target = arguments[self.target_field]
        if type(target) is not str or not target.strip():
            raise ValueError("resolved target must be a non-empty string")
        return ResolvedEffect(
            resource_type=self.resource_type,
            target=target,
            effect_class=self.effect_class,
            parameters={
                key: value
                for key, value in arguments.items()
                if key != self.target_field
            },
        )


_PRODUCT_EXTENSIONS = (
    (
        "git.observe",
        "product.git.observe.v1",
        Capability(
            capability_id="git.observe",
            schema_version=1,
            operation="observe",
            resource_type="git_repository",
            effect_class=EffectClass.OBSERVE,
            reversibility=Reversibility.REVERSIBLE,
            risk_class=RiskClass.LOW,
            input_schema=_object_schema(
                {
                    "path": _string_schema(),
                    "operation": _string_schema(enum=("status", "diff", "log")),
                },
                ("path", "operation"),
            ),
            output_schema=_open_output_schema(),
            binding_revision=0,
            handler_binding="product.git.observe.v1",
        ),
        TargetResolver("git_repository", EffectClass.OBSERVE, "path"),
    ),
    (
        "structured.json.read",
        "product.structured.json.read.v1",
        Capability(
            capability_id="structured.json.read",
            schema_version=1,
            operation="read",
            resource_type="structured_data_path",
            effect_class=EffectClass.OBSERVE,
            reversibility=Reversibility.REVERSIBLE,
            risk_class=RiskClass.LOW,
            input_schema=_object_schema({"path": _string_schema()}, ("path",)),
            output_schema=_open_output_schema(),
            binding_revision=0,
            handler_binding="product.structured.json.read.v1",
        ),
        TargetResolver("structured_data_path", EffectClass.OBSERVE, "path"),
    ),
    (
        "structured.json.write",
        "product.structured.json.write.v1",
        Capability(
            capability_id="structured.json.write",
            schema_version=1,
            operation="write",
            resource_type="structured_data_path",
            effect_class=EffectClass.MODIFY,
            reversibility=Reversibility.COMPENSATABLE,
            risk_class=RiskClass.MEDIUM,
            input_schema=_object_schema(
                {"path": _string_schema(), "json": {"type": "string"}},
                ("path", "json"),
            ),
            output_schema=_open_output_schema(),
            binding_revision=0,
            handler_binding="product.structured.json.write.v1",
        ),
        TargetResolver("structured_data_path", EffectClass.MODIFY, "path"),
    ),
)


PRODUCT_CAPABILITY_IDS = (
    "workspace.read",
    "workspace.list",
    "workspace.write",
    "command.observe",
    "network.fetch",
    "git.observe",
    "structured.json.read",
    "structured.json.write",
    "artifact.write",
)

_CAPABILITY_FAMILIES = {
    "workspace.read": "filesystem",
    "workspace.list": "filesystem",
    "workspace.write": "filesystem",
    "command.observe": "shell",
    "network.fetch": "http",
    "git.observe": "git",
    "structured.json.read": "structured_data",
    "structured.json.write": "structured_data",
    "artifact.write": "artifact_generation",
}


def install_product_capabilities(
    capabilities: CapabilityRepository,
    handlers: CapabilityHandlerRegistry,
) -> None:
    install_builtin_capabilities(capabilities, handlers)
    for capability_id, binding_id, capability, resolver in _PRODUCT_EXTENSIONS:
        try:
            current = capabilities.get(capability_id)
        except UnknownCapability:
            current = capabilities.register(capability)
        if current != capability:
            raise IntegrityViolation(
                f"durable product Capability differs from bootstrap contract: {capability_id}"
            )
        if handlers.contains(binding_id):
            if handlers.resolve(binding_id) != resolver:
                raise IntegrityViolation(
                    f"runtime handler differs from product binding contract: {binding_id}"
                )
            continue
        handlers.register(binding_id, resolver)


def capability_family(capability_id: str) -> str:
    try:
        return _CAPABILITY_FAMILIES[capability_id]
    except KeyError as exc:
        raise UnknownCapability(capability_id) from exc
