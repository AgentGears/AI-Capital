from __future__ import annotations

import shlex
from dataclasses import dataclass

from .capability_broker import CapabilityHandlerRegistry
from .capability_store import CapabilityRepository
from .enums import EffectClass, Reversibility, RiskClass
from .errors import IntegrityViolation, UnknownCapability
from .frozen_json import FrozenMap
from .models import Capability, ResolvedEffect


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


def _string_schema() -> dict[str, object]:
    return {"type": "string", "min_length": 1}


def _open_output_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {},
        "required": (),
        "additional_properties": True,
    }


@dataclass(frozen=True, slots=True)
class TargetFieldResolver:
    resource_type: str
    effect_class: EffectClass
    target_field: str

    def resolve_effect(self, arguments: FrozenMap) -> ResolvedEffect:
        target = arguments[self.target_field]
        if type(target) is not str or not target.strip():
            raise ValueError("resolved target must be a non-empty string")
        parameters = {
            key: value
            for key, value in arguments.items()
            if key != self.target_field
        }
        return ResolvedEffect(
            resource_type=self.resource_type,
            target=target,
            effect_class=self.effect_class,
            parameters=parameters,
        )


_READ_ONLY_COMMANDS = frozenset({"pwd", "ls", "cat", "head", "tail", "wc", "stat"})
_FORBIDDEN_COMMAND_SYNTAX = frozenset("|&;<>()$`\n\r")


@dataclass(frozen=True, slots=True)
class CommandObserveResolver:
    """Conservatively admits only a small shell-free observation subset."""

    def resolve_effect(self, arguments: FrozenMap) -> ResolvedEffect:
        command = arguments["command"]
        if type(command) is not str or not command.strip():
            raise ValueError("command must be a non-empty string")
        if any(token in command for token in _FORBIDDEN_COMMAND_SYNTAX):
            raise ValueError("command.observe forbids shell control or substitution syntax")
        try:
            parts = shlex.split(command, posix=True)
        except ValueError as exc:
            raise ValueError("command.observe command cannot be parsed safely") from exc
        if not parts or parts[0] not in _READ_ONLY_COMMANDS:
            raise ValueError("command.observe permits only the read-only command subset")
        return ResolvedEffect(
            resource_type="command",
            target=command,
            effect_class=EffectClass.OBSERVE,
            parameters={},
        )


_BUILTINS = (
    (
        "workspace.read",
        "builtin.workspace.read.v1",
        Capability(
            capability_id="workspace.read",
            schema_version=1,
            operation="read",
            resource_type="workspace_path",
            effect_class=EffectClass.OBSERVE,
            reversibility=Reversibility.REVERSIBLE,
            risk_class=RiskClass.LOW,
            input_schema=_object_schema({"path": _string_schema()}, ("path",)),
            output_schema=_open_output_schema(),
            binding_revision=0,
            handler_binding="builtin.workspace.read.v1",
        ),
        TargetFieldResolver("workspace_path", EffectClass.OBSERVE, "path"),
    ),
    (
        "workspace.list",
        "builtin.workspace.list.v1",
        Capability(
            capability_id="workspace.list",
            schema_version=1,
            operation="list",
            resource_type="workspace_path",
            effect_class=EffectClass.OBSERVE,
            reversibility=Reversibility.REVERSIBLE,
            risk_class=RiskClass.LOW,
            input_schema=_object_schema({"path": _string_schema()}, ("path",)),
            output_schema=_open_output_schema(),
            binding_revision=0,
            handler_binding="builtin.workspace.list.v1",
        ),
        TargetFieldResolver("workspace_path", EffectClass.OBSERVE, "path"),
    ),
    (
        "workspace.write",
        "builtin.workspace.write.v1",
        Capability(
            capability_id="workspace.write",
            schema_version=1,
            operation="write",
            resource_type="workspace_path",
            effect_class=EffectClass.MODIFY,
            reversibility=Reversibility.COMPENSATABLE,
            risk_class=RiskClass.MEDIUM,
            input_schema=_object_schema(
                {"path": _string_schema(), "content": {"type": "string"}},
                ("path", "content"),
            ),
            output_schema=_open_output_schema(),
            binding_revision=0,
            handler_binding="builtin.workspace.write.v1",
        ),
        TargetFieldResolver("workspace_path", EffectClass.MODIFY, "path"),
    ),
    (
        "workspace.patch",
        "builtin.workspace.patch.v1",
        Capability(
            capability_id="workspace.patch",
            schema_version=1,
            operation="patch",
            resource_type="workspace_path",
            effect_class=EffectClass.MODIFY,
            reversibility=Reversibility.COMPENSATABLE,
            risk_class=RiskClass.MEDIUM,
            input_schema=_object_schema(
                {"path": _string_schema(), "patch": _string_schema()},
                ("path", "patch"),
            ),
            output_schema=_open_output_schema(),
            binding_revision=0,
            handler_binding="builtin.workspace.patch.v1",
        ),
        TargetFieldResolver("workspace_path", EffectClass.MODIFY, "path"),
    ),
    (
        "command.observe",
        "builtin.command.observe.v1",
        Capability(
            capability_id="command.observe",
            schema_version=1,
            operation="observe",
            resource_type="command",
            effect_class=EffectClass.OBSERVE,
            reversibility=Reversibility.REVERSIBLE,
            risk_class=RiskClass.MEDIUM,
            input_schema=_object_schema({"command": _string_schema()}, ("command",)),
            output_schema=_open_output_schema(),
            binding_revision=0,
            handler_binding="builtin.command.observe.v1",
        ),
        CommandObserveResolver(),
    ),
    (
        "network.fetch",
        "builtin.network.fetch.v1",
        Capability(
            capability_id="network.fetch",
            schema_version=1,
            operation="fetch",
            resource_type="network_resource",
            effect_class=EffectClass.OBSERVE,
            reversibility=Reversibility.REVERSIBLE,
            risk_class=RiskClass.MEDIUM,
            input_schema=_object_schema({"url": _string_schema()}, ("url",)),
            output_schema=_open_output_schema(),
            binding_revision=0,
            handler_binding="builtin.network.fetch.v1",
        ),
        TargetFieldResolver("network_resource", EffectClass.OBSERVE, "url"),
    ),
    (
        "artifact.write",
        "builtin.artifact.write.v1",
        Capability(
            capability_id="artifact.write",
            schema_version=1,
            operation="write",
            resource_type="artifact_path",
            effect_class=EffectClass.CREATE,
            reversibility=Reversibility.REVERSIBLE,
            risk_class=RiskClass.LOW,
            input_schema=_object_schema(
                {"path": _string_schema(), "content": {"type": "string"}},
                ("path", "content"),
            ),
            output_schema=_open_output_schema(),
            binding_revision=0,
            handler_binding="builtin.artifact.write.v1",
        ),
        TargetFieldResolver("artifact_path", EffectClass.CREATE, "path"),
    ),
)


def install_builtin_capabilities(
    capabilities: CapabilityRepository,
    handlers: CapabilityHandlerRegistry,
) -> None:
    for capability_id, binding_id, capability, resolver in _BUILTINS:
        try:
            current = capabilities.get(capability_id)
        except UnknownCapability:
            current = capabilities.register(capability)
        if current != capability:
            raise IntegrityViolation(
                f"durable built-in Capability differs from bootstrap contract: {capability_id}"
            )
        if handlers.contains(binding_id):
            if handlers.resolve(binding_id) != resolver:
                raise IntegrityViolation(
                    f"runtime handler differs from built-in binding contract: {binding_id}"
                )
            continue
        handlers.register(binding_id, resolver)


def builtin_capability_ids() -> tuple[str, ...]:
    return tuple(item[0] for item in _BUILTINS)
