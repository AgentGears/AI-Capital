from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_capital.kernel.actor_store import ActorRepository
from ai_capital.kernel.authority import AuthorityEngine, PolicySnapshot
from ai_capital.kernel.authority_store import AuthorityRepository
from ai_capital.kernel.builtin_capabilities import install_builtin_capabilities
from ai_capital.kernel.capability_broker import CapabilityBroker, CapabilityHandlerRegistry
from ai_capital.kernel.capability_store import CapabilityRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import AuthorityDecisionKind, EffectClass, ProgramStatus, RiskClass
from ai_capital.kernel.errors import IntegrityViolation
from ai_capital.kernel.models import Actor, CapabilityRequest, Grant, Program
from ai_capital.kernel.schema_codec import record_to_json
from ai_capital.kernel.serialization import canonical_digest


NOW = "2026-08-30T00:00:00Z"


class StoredDecisionIntegrityTests(unittest.TestCase):
    def test_forged_stored_allow_with_valid_digest_cannot_bypass_ask_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kernel.db"
            with ProgramRepository(path) as programs:
                programs.create(Program("p-1", 0, "stored decision integrity"))
                programs.transition("p-1", ProgramStatus.ACTIVE, expected_revision=0)
                actors = ActorRepository(programs)
                actors.register(Actor("a-1", 0, "worker", "binding-a"))

                capabilities = CapabilityRepository(programs)
                handlers = CapabilityHandlerRegistry()
                install_builtin_capabilities(capabilities, handlers)
                broker = CapabilityBroker(capabilities, handlers)

                authority = AuthorityRepository(programs)
                authority.install_policy(
                    PolicySnapshot(0, (RiskClass.LOW,), (), NOW)
                )
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
                original = engine.decide(
                    program_id="p-1",
                    actor_id="a-1",
                    resolution=resolution,
                )
                self.assertIs(original.decision.decision, AuthorityDecisionKind.ASK)

                forged_decision = replace(
                    original.decision,
                    decision=AuthorityDecisionKind.ALLOW,
                    rationale_code="grant_and_policy_allow",
                )
                forged_context = replace(original, decision=forged_decision)
                programs._db.execute(
                    """
                    UPDATE authority_decisions
                    SET context_json = ?, context_digest = ?
                    WHERE decision_id = ?
                    """,
                    (
                        record_to_json(forged_context),
                        canonical_digest(forged_context),
                        forged_decision.decision_id,
                    ),
                )

                with self.assertRaises(IntegrityViolation):
                    engine.issue_execution_authority(
                        decision_id=forged_decision.decision_id
                    )


if __name__ == "__main__":
    unittest.main()
