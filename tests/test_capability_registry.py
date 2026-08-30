from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_capital.kernel.builtin_capabilities import (
    builtin_capability_ids,
    install_builtin_capabilities,
)
from ai_capital.kernel.capability_broker import (
    CapabilityBroker,
    CapabilityHandlerRegistry,
)
from ai_capital.kernel.capability_store import (
    CapabilityRepository,
    capability_descriptor,
)
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import EffectClass, Reversibility, RiskClass
from ai_capital.kernel.errors import (
    CapabilityUnavailable,
    IntegrityViolation,
    InvalidRequest,
    StaleCapabilityBinding,
    UnknownCapability,
)
from ai_capital.kernel.frozen_json import FrozenMap
from ai_capital.kernel.models import Capability, CapabilityRequest, ResolvedEffect


def simple_capability(handler_binding: str = "handler-a") -> Capability:
    return Capability(
        capability_id="workspace.read",
        schema_version=1,
        operation="read",
        resource_type="workspace_path",
        effect_class=EffectClass.OBSERVE,
        reversibility=Reversibility.REVERSIBLE,
        risk_class=RiskClass.LOW,
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string", "min_length": 1}},
            "required": ("path",),
            "additional_properties": False,
        },
        output_schema={
            "type": "object",
            "properties": {},
            "required": (),
            "additional_properties": True,
        },
        binding_revision=0,
        handler_binding=handler_binding,
    )


class CountingResolver:
    def __init__(self, binding_target: str = "path"):
        self.calls = 0
        self.binding_target = binding_target

    def resolve_effect(self, arguments: FrozenMap) -> ResolvedEffect:
        self.calls += 1
        return ResolvedEffect(
            resource_type="workspace_path",
            target=arguments[self.binding_target],
            effect_class=EffectClass.OBSERVE,
            parameters={},
        )


class WrongContractResolver:
    def resolve_effect(self, arguments: FrozenMap) -> ResolvedEffect:
        return ResolvedEffect(
            resource_type="different_resource",
            target=arguments["path"],
            effect_class=EffectClass.MODIFY,
            parameters={},
        )


class CapabilityRegistryTests(unittest.TestCase):
    def test_unknown_capability_fails_loud(self):
        with tempfile.TemporaryDirectory() as directory:
            with ProgramRepository(Path(directory) / "kernel.db") as host:
                capabilities = CapabilityRepository(host)
                with self.assertRaises(UnknownCapability):
                    capabilities.get("missing")

    def test_invalid_input_fails_before_effect_resolver(self):
        with tempfile.TemporaryDirectory() as directory:
            with ProgramRepository(Path(directory) / "kernel.db") as host:
                capabilities = CapabilityRepository(host)
                handlers = CapabilityHandlerRegistry()
                capabilities.register(simple_capability())
                resolver = CountingResolver()
                handlers.register("handler-a", resolver)
                broker = CapabilityBroker(capabilities, handlers)
                snapshot = broker.snapshot(("workspace.read",))

                with self.assertRaises(InvalidRequest):
                    broker.resolve(
                        CapabilityRequest("req-1", "workspace.read", {}, 0),
                        snapshot=snapshot,
                    )
                self.assertEqual(resolver.calls, 0)

    def test_changed_binding_revision_invalidates_stale_request(self):
        with tempfile.TemporaryDirectory() as directory:
            with ProgramRepository(Path(directory) / "kernel.db") as host:
                capabilities = CapabilityRepository(host)
                handlers = CapabilityHandlerRegistry()
                capabilities.register(simple_capability())
                handlers.register("handler-a", CountingResolver())
                handlers.register("handler-b", CountingResolver())
                broker = CapabilityBroker(capabilities, handlers)
                stale_snapshot = broker.snapshot(("workspace.read",))
                capabilities.replace_handler(
                    "workspace.read", "handler-b", expected_binding_revision=0
                )

                with self.assertRaises(StaleCapabilityBinding):
                    broker.resolve(
                        CapabilityRequest(
                            "req-1",
                            "workspace.read",
                            {"path": "a.txt"},
                            0,
                        ),
                        snapshot=stale_snapshot,
                    )

    def test_availability_is_observable_without_permission_or_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            with ProgramRepository(Path(directory) / "kernel.db") as host:
                capabilities = CapabilityRepository(host)
                handlers = CapabilityHandlerRegistry()
                capabilities.register(simple_capability())
                handlers.register("handler-a", CountingResolver())
                broker = CapabilityBroker(capabilities, handlers)

                available = broker.available_descriptors()
                self.assertEqual(tuple(item.capability_id for item in available), ("workspace.read",))
                self.assertFalse(hasattr(broker, "execute"))
                self.assertFalse(hasattr(broker, "authorize"))

    def test_unbound_capability_is_declared_but_not_available(self):
        with tempfile.TemporaryDirectory() as directory:
            with ProgramRepository(Path(directory) / "kernel.db") as host:
                capabilities = CapabilityRepository(host)
                handlers = CapabilityHandlerRegistry()
                capabilities.register(simple_capability())
                broker = CapabilityBroker(capabilities, handlers)
                self.assertEqual(broker.available_descriptors(), ())
                with self.assertRaises(CapabilityUnavailable):
                    broker.snapshot(("workspace.read",))

    def test_handler_substitution_preserves_semantic_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kernel.db"
            with ProgramRepository(path) as host:
                capabilities = CapabilityRepository(host)
                original = capabilities.register(simple_capability())
                before = capability_descriptor(original)
                updated = capabilities.replace_handler(
                    "workspace.read", "handler-b", expected_binding_revision=0
                )
                after = capability_descriptor(updated)
                self.assertEqual(
                    replace(after, binding_revision=before.binding_revision),
                    before,
                )
                self.assertEqual(updated.binding_revision, 1)
                self.assertEqual(
                    tuple(item.binding_revision for item in capabilities.bindings("workspace.read")),
                    (0, 1),
                )

            with ProgramRepository(path) as restarted:
                capabilities = CapabilityRepository(restarted)
                self.assertEqual(capabilities.get("workspace.read").binding_revision, 1)

    def test_descriptor_hides_handler_binding(self):
        descriptor = capability_descriptor(simple_capability())
        self.assertFalse(hasattr(descriptor, "handler_binding"))
        self.assertEqual(descriptor.operation, "read")
        self.assertEqual(descriptor.resource_type, "workspace_path")

    def test_resolution_returns_semantic_effect_without_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            with ProgramRepository(Path(directory) / "kernel.db") as host:
                capabilities = CapabilityRepository(host)
                handlers = CapabilityHandlerRegistry()
                capabilities.register(simple_capability())
                resolver = CountingResolver()
                handlers.register("handler-a", resolver)
                broker = CapabilityBroker(capabilities, handlers)
                snapshot = broker.snapshot(("workspace.read",))
                resolution = broker.resolve(
                    CapabilityRequest(
                        "req-1",
                        "workspace.read",
                        {"path": "notes.txt"},
                        0,
                    ),
                    snapshot=snapshot,
                )
                self.assertEqual(resolver.calls, 1)
                self.assertEqual(resolution.resolved_effect.target, "notes.txt")
                self.assertEqual(resolution.resolved_effect.effect_class, EffectClass.OBSERVE)
                self.assertEqual(resolution.request_id, "req-1")

    def test_resolver_cannot_escape_capability_semantic_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            with ProgramRepository(Path(directory) / "kernel.db") as host:
                capabilities = CapabilityRepository(host)
                handlers = CapabilityHandlerRegistry()
                capabilities.register(simple_capability())
                handlers.register("handler-a", WrongContractResolver())
                broker = CapabilityBroker(capabilities, handlers)
                snapshot = broker.snapshot(("workspace.read",))
                with self.assertRaises(IntegrityViolation):
                    broker.resolve(
                        CapabilityRequest(
                            "req-1", "workspace.read", {"path": "notes.txt"}, 0
                        ),
                        snapshot=snapshot,
                    )

    def test_snapshot_store_rejects_noncurrent_or_forged_descriptor(self):
        with tempfile.TemporaryDirectory() as directory:
            with ProgramRepository(Path(directory) / "kernel.db") as host:
                capabilities = CapabilityRepository(host)
                registered = capabilities.register(simple_capability())
                descriptor = capability_descriptor(registered)
                forged = replace(descriptor, operation="different-operation")
                with self.assertRaises(InvalidRequest):
                    capabilities.create_snapshot((forged,))

                capabilities.replace_handler(
                    "workspace.read", "handler-b", expected_binding_revision=0
                )
                with self.assertRaises(InvalidRequest):
                    capabilities.create_snapshot((descriptor,))

    def test_initial_builtin_family_is_complete_and_snapshot_is_durable(self):
        expected = (
            "workspace.read",
            "workspace.list",
            "workspace.write",
            "workspace.patch",
            "command.observe",
            "network.fetch",
            "artifact.write",
        )
        self.assertEqual(builtin_capability_ids(), expected)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kernel.db"
            with ProgramRepository(path) as host:
                capabilities = CapabilityRepository(host)
                handlers = CapabilityHandlerRegistry()
                install_builtin_capabilities(capabilities, handlers)
                broker = CapabilityBroker(capabilities, handlers)
                snapshot = broker.snapshot()
                self.assertEqual(
                    tuple(item.capability_id for item in snapshot.capabilities),
                    tuple(sorted(expected)),
                )
                snapshot_id = snapshot.snapshot_id

            with ProgramRepository(path) as restarted:
                capabilities = CapabilityRepository(restarted)
                restored = capabilities.get_snapshot(snapshot_id)
                self.assertEqual(restored.snapshot_id, snapshot_id)
                self.assertEqual(len(restored.capabilities), len(expected))


if __name__ == "__main__":
    unittest.main()
