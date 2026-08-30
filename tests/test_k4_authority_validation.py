from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_capital.kernel.actor_store import ActorRepository
from ai_capital.kernel.authority import (
    AuthorityEngine,
    PolicySnapshot,
    effect_allowed_by_ceiling,
)
from ai_capital.kernel.authority_store import AuthorityRepository
from ai_capital.kernel.builtin_capabilities import install_builtin_capabilities
from ai_capital.kernel.capability_broker import CapabilityBroker, CapabilityHandlerRegistry
from ai_capital.kernel.capability_store import CapabilityRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ActorStatus, EffectClass, ProgramStatus
from ai_capital.kernel.errors import AuthorityDenied, InvalidRequest
from ai_capital.kernel.models import Actor, CapabilityRequest, Grant, Program
from ai_capital.kernel.schema_codec import record_to_json
from ai_capital.kernel.serialization import canonical_digest


NOW = "2026-08-30T00:00:00Z"


class K4AuthorityValidationTests(unittest.TestCase):
    def test_effect_ceiling_is_monotonic(self):
        self.assertTrue(
            effect_allowed_by_ceiling(EffectClass.DELETE, EffectClass.OBSERVE)
        )
        self.assertTrue(
            effect_allowed_by_ceiling(EffectClass.DELETE, EffectClass.CREATE)
        )
        self.assertTrue(
            effect_allowed_by_ceiling(EffectClass.DELETE, EffectClass.MODIFY)
        )
        self.assertTrue(
            effect_allowed_by_ceiling(EffectClass.DELETE, EffectClass.DELETE)
        )
        self.assertFalse(
            effect_allowed_by_ceiling(
                EffectClass.DELETE, EffectClass.EXTERNAL_SIDE_EFFECT
            )
        )
        self.assertFalse(
            effect_allowed_by_ceiling(EffectClass.MODIFY, EffectClass.DELETE)
        )

    def test_grant_rejects_invalid_or_non_increasing_timestamps(self):
        with tempfile.TemporaryDirectory() as directory:
            with ProgramRepository(Path(directory) / "kernel.db") as programs:
                authority = AuthorityRepository(programs)
                with self.assertRaises(InvalidRequest):
                    authority.issue_grant(
                        Grant(
                            "g-invalid",
                            "actor:a-1",
                            ("workspace.read",),
                            ("*",),
                            EffectClass.OBSERVE,
                            (),
                            "not-a-time",
                            None,
                            0,
                        )
                    )
                with self.assertRaises(InvalidRequest):
                    authority.issue_grant(
                        Grant(
                            "g-naive",
                            "actor:a-1",
                            ("workspace.read",),
                            ("*",),
                            EffectClass.OBSERVE,
                            (),
                            "2026-08-30T00:00:00",
                            None,
                            0,
                        )
                    )
                with self.assertRaises(InvalidRequest):
                    authority.issue_grant(
                        Grant(
                            "g-expiry",
                            "actor:a-1",
                            ("workspace.read",),
                            ("*",),
                            EffectClass.OBSERVE,
                            (),
                            "2026-08-30T00:00:00Z",
                            "2026-08-30T00:00:00Z",
                            0,
                        )
                    )

    def test_policy_rejects_naive_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            with ProgramRepository(Path(directory) / "kernel.db") as programs:
                authority = AuthorityRepository(programs)
                with self.assertRaises(InvalidRequest):
                    authority.install_policy(
                        PolicySnapshot(0, (), (), "2026-08-30T00:00:00")
                    )

    def test_disabled_actor_is_rejected_without_generation_change(self):
        with tempfile.TemporaryDirectory() as directory:
            with ProgramRepository(Path(directory) / "kernel.db") as programs:
                programs.create(Program("p-1", 0, "inactive actor proof"))
                programs.transition("p-1", ProgramStatus.ACTIVE, expected_revision=0)

                actors = ActorRepository(programs)
                actor = actors.register(Actor("a-1", 0, "worker", "binding-a"))
                disabled = replace(actor, status=ActorStatus.DISABLED)
                programs._db.execute(
                    """
                    UPDATE actor_projections
                    SET actor_json = ?, actor_digest = ?
                    WHERE actor_id = ? AND generation = ?
                    """,
                    (
                        record_to_json(disabled),
                        canonical_digest(disabled),
                        disabled.actor_id,
                        disabled.generation,
                    ),
                )

                capabilities = CapabilityRepository(programs)
                handlers = CapabilityHandlerRegistry()
                install_builtin_capabilities(capabilities, handlers)
                broker = CapabilityBroker(capabilities, handlers)

                authority = AuthorityRepository(programs)
                authority.install_policy(PolicySnapshot(0, (), (), NOW))
                authority.issue_grant(
                    Grant(
                        "g-1",
                        "actor:a-1",
                        ("workspace.read",),
                        ("*",),
                        EffectClass.OBSERVE,
                        (),
                        NOW,
                        None,
                        0,
                    )
                )

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
                engine = AuthorityEngine(programs, actors, capabilities, authority)

                with self.assertRaises(AuthorityDenied):
                    engine.decide(
                        program_id="p-1",
                        actor_id="a-1",
                        resolution=resolution,
                    )


if __name__ == "__main__":
    unittest.main()
