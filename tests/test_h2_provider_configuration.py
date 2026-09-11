from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from ai_capital.kernel.actor_store import ActorRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ProgramStatus
from ai_capital.kernel.errors import (
    IntegrityViolation,
    InvalidRequest,
    StaleActorGeneration,
    StaleProviderConfigurationRevision,
)
from ai_capital.kernel.models import Actor, Program
from ai_capital.product import LocalActorProviderOperator, LocalProviderOperator


class H2ProviderConfigurationTests(unittest.TestCase):
    def test_provider_configuration_survives_restart_with_immutable_history(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "capital.db"
            with LocalProviderOperator.open(database) as providers:
                registered = providers.register(
                    binding_id="binding-a",
                    adapter="adapter-a",
                    model="model-a",
                    settings={"temperature": 0.2, "max_output_units": 400},
                )
                self.assertEqual(registered["revision"], 0)
                updated = providers.update(
                    "binding-a",
                    expected_revision=0,
                    adapter="adapter-b",
                    model="model-b",
                    settings={"reasoning_effort": "high", "response_format": "json"},
                )
                self.assertEqual(updated["revision"], 1)
                with self.assertRaises(StaleProviderConfigurationRevision):
                    providers.update(
                        "binding-a",
                        expected_revision=0,
                        adapter="adapter-c",
                        model="model-c",
                    )
                self.assertEqual(providers.show("binding-a"), updated)

            with LocalProviderOperator.open(database) as restarted:
                self.assertEqual(restarted.list(), (updated,))
                history = restarted.history("binding-a")
                self.assertEqual(tuple(item["revision"] for item in history), (0, 1))
                self.assertEqual(history[0], registered)
                self.assertEqual(history[1], updated)

    def test_provider_configuration_rejects_secret_capable_or_invalid_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "capital.db"
            with LocalProviderOperator.open(database) as providers:
                for settings in (
                    {"api_key": "not-permitted"},
                    {"endpoint_token": "not-permitted"},
                    {"temperature": 3},
                    {"top_p": -0.1},
                    {"max_output_units": 0},
                    {"reasoning_effort": "extreme"},
                ):
                    with self.subTest(settings=settings):
                        with self.assertRaises(InvalidRequest):
                            providers.register(
                                binding_id=f"binding-{len(str(settings))}",
                                adapter="adapter",
                                model="model",
                                settings=settings,
                            )
                self.assertEqual(providers.list(), ())

    def test_provider_projection_corruption_fails_closed_and_history_is_immutable(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "capital.db"
            with LocalProviderOperator.open(database) as providers:
                providers.register(
                    binding_id="binding-a",
                    adapter="adapter",
                    model="model-a",
                )
                updated = providers.update(
                    "binding-a",
                    expected_revision=0,
                    adapter="adapter",
                    model="model-b",
                )
                providers._programs._db.execute(
                    """
                    UPDATE provider_configuration_projections
                    SET configuration_digest = ? WHERE binding_id = ?
                    """,
                    ("0" * 64, "binding-a"),
                )
                with self.assertRaises(IntegrityViolation):
                    providers.show("binding-a")
                providers._programs._db.execute(
                    """
                    UPDATE provider_configuration_projections
                    SET configuration_digest = (
                        SELECT configuration_digest
                        FROM provider_configuration_revisions
                        WHERE binding_id = ? AND revision = 1
                    ) WHERE binding_id = ?
                    """,
                    ("binding-a", "binding-a"),
                )
                self.assertEqual(providers.show("binding-a"), updated)
                with self.assertRaises(sqlite3.IntegrityError):
                    providers._programs._db.execute(
                        """
                        UPDATE provider_configuration_revisions
                        SET configuration_digest = ?
                        WHERE binding_id = ? AND revision = 0
                        """,
                        ("0" * 64, "binding-a"),
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    providers._programs._db.execute(
                        """
                        DELETE FROM provider_configuration_revisions
                        WHERE binding_id = ? AND revision = 0
                        """,
                        ("binding-a",),
                    )

    def test_projection_deletion_is_blocked_and_missing_projection_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "capital.db"
            with LocalProviderOperator.open(database) as providers:
                providers.register(
                    binding_id="binding-a",
                    adapter="adapter",
                    model="model-a",
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    providers._programs._db.execute(
                        """
                        DELETE FROM provider_configuration_projections
                        WHERE binding_id = ?
                        """,
                        ("binding-a",),
                    )
                providers._programs._db.execute(
                    "DROP TRIGGER provider_configuration_projection_no_delete"
                )
                providers._programs._db.execute(
                    """
                    DELETE FROM provider_configuration_projections
                    WHERE binding_id = ?
                    """,
                    ("binding-a",),
                )
                with self.assertRaises(IntegrityViolation):
                    providers.list()
                with self.assertRaises(IntegrityViolation):
                    providers.show("binding-a")

    def test_non_integer_provider_revision_scalar_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "capital.db"
            with LocalProviderOperator.open(database) as providers:
                providers.register(
                    binding_id="binding-a",
                    adapter="adapter",
                    model="model-a",
                )
                providers._programs._db.execute(
                    """
                    UPDATE provider_configuration_projections
                    SET revision = 0.5 WHERE binding_id = ?
                    """,
                    ("binding-a",),
                )
                with self.assertRaises(IntegrityViolation):
                    providers.show("binding-a")

    def test_existing_schema_marker_reauthenticates_provider_table_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "capital.db"
            with LocalProviderOperator.open(database) as providers:
                providers.register(
                    binding_id="binding-a",
                    adapter="adapter",
                    model="model",
                )
            with ProgramRepository(database) as programs:
                programs._db.execute("DROP TABLE provider_configuration_projections")
            with LocalProviderOperator.open(database) as restarted:
                with self.assertRaises(IntegrityViolation):
                    restarted.list()

    def test_malformed_provider_schema_version_fails_closed_on_product_surfaces(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "capital.db"
            with ProgramRepository(database) as programs:
                ActorRepository(programs).register(
                    Actor("a-1", 0, "worker", "legacy-binding")
                )
            with LocalProviderOperator.open(database) as providers:
                providers.register(
                    binding_id="binding-a",
                    adapter="adapter",
                    model="model",
                )
            with ProgramRepository(database) as programs:
                programs._db.execute(
                    """
                    UPDATE component_schema SET version = 1.5
                    WHERE component = 'product_provider_configuration'
                    """
                )
            with LocalProviderOperator.open(database) as providers:
                with self.assertRaises(IntegrityViolation):
                    providers.list()
            with LocalActorProviderOperator.open(database) as actor_provider:
                with self.assertRaises(IntegrityViolation):
                    actor_provider.show("a-1")

    def test_read_only_provider_listing_does_not_bootstrap_provider_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "capital.db"
            with LocalProviderOperator.open(database) as providers:
                self.assertEqual(providers.list(), ())
                table = providers._programs._db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'component_schema'"
                ).fetchone()
                if table is not None:
                    row = providers._programs._db.execute(
                        """
                        SELECT 1 FROM component_schema
                        WHERE component = 'product_provider_configuration'
                        """
                    ).fetchone()
                    self.assertIsNone(row)

    def test_registration_cannot_retroactively_adopt_matching_legacy_actor_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "capital.db"
            with ProgramRepository(database) as programs:
                ActorRepository(programs).register(
                    Actor("a-1", 0, "worker", "binding-a")
                )

            with LocalProviderOperator.open(database) as providers:
                registered = providers.register(
                    binding_id="binding-a",
                    adapter="adapter",
                    model="model-a",
                )
                self.assertNotEqual(registered["model_binding"], "binding-a")
                self.assertTrue(
                    registered["model_binding"].startswith("provider-binding:")
                )

            with LocalActorProviderOperator.open(database) as actor_provider:
                unchanged = actor_provider.show("a-1")
                self.assertEqual(unchanged["actor"]["generation"], 0)
                self.assertEqual(unchanged["actor"]["model_binding"], "binding-a")
                self.assertFalse(unchanged["configured"])
                self.assertIsNone(unchanged["provider"])

                rebound = actor_provider.rebind(
                    "a-1",
                    "binding-a",
                    expected_generation=0,
                    expected_provider_revision=0,
                )
                self.assertEqual(rebound["actor"]["generation"], 1)
                self.assertEqual(
                    rebound["actor"]["model_binding"],
                    registered["model_binding"],
                )
                self.assertEqual(rebound["provider"], registered)
                self.assertTrue(rebound["configured"])

    def test_actor_rebind_preserves_identity_program_and_non_binding_actor_state(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "capital.db"
            with ProgramRepository(database) as programs:
                programs.create(Program("p-1", 0, "continue across provider replacement"))
                programs.transition("p-1", ProgramStatus.ACTIVE, expected_revision=0)
                actors = ActorRepository(programs)
                before_actor = actors.register(
                    Actor(
                        "a-1",
                        0,
                        "worker",
                        "legacy-binding",
                        grant_refs=("grant-1",),
                    )
                )
                before_program = programs.get("p-1")
                before_events = programs.list_events("p-1")

            with LocalProviderOperator.open(database) as providers:
                provider = providers.register(
                    binding_id="binding-new",
                    adapter="adapter",
                    model="model-new",
                )

            with LocalActorProviderOperator.open(database) as actor_provider:
                changed = actor_provider.rebind(
                    "a-1",
                    "binding-new",
                    expected_generation=0,
                    expected_provider_revision=provider["revision"],
                )
                actor = changed["actor"]
                self.assertEqual(actor["actor_id"], before_actor.actor_id)
                self.assertEqual(actor["generation"], 1)
                self.assertEqual(actor["profile"], before_actor.profile)
                self.assertEqual(actor["status"], before_actor.status.value)
                self.assertEqual(actor["grant_refs"], list(before_actor.grant_refs))
                self.assertEqual(actor["model_binding"], provider["model_binding"])
                self.assertTrue(changed["configured"])
                self.assertEqual(changed["provider"], provider)

            with ProgramRepository(database) as programs:
                self.assertEqual(programs.get("p-1"), before_program)
                self.assertEqual(programs.list_events("p-1"), before_events)
                actors = ActorRepository(programs)
                generations = actors.generations("a-1")
                self.assertEqual(tuple(item.generation for item in generations), (0, 1))

    def test_stale_provider_or_actor_revision_fails_without_partial_rebind(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "capital.db"
            with ProgramRepository(database) as programs:
                actors = ActorRepository(programs)
                actors.register(Actor("a-1", 0, "worker", "legacy-binding"))
            with LocalProviderOperator.open(database) as providers:
                providers.register(
                    binding_id="binding-new",
                    adapter="adapter",
                    model="model-a",
                )
                providers.update(
                    "binding-new",
                    expected_revision=0,
                    adapter="adapter",
                    model="model-b",
                )

            with LocalActorProviderOperator.open(database) as actor_provider:
                with self.assertRaises(StaleProviderConfigurationRevision):
                    actor_provider.rebind(
                        "a-1",
                        "binding-new",
                        expected_generation=0,
                        expected_provider_revision=0,
                    )
                self.assertEqual(
                    actor_provider.show("a-1")["actor"]["model_binding"],
                    "legacy-binding",
                )
                with self.assertRaises(StaleActorGeneration):
                    actor_provider.rebind(
                        "a-1",
                        "binding-new",
                        expected_generation=9,
                        expected_provider_revision=1,
                    )
                current = actor_provider.show("a-1")["actor"]
                self.assertEqual(current["generation"], 0)
                self.assertEqual(current["model_binding"], "legacy-binding")

    def test_actor_inspection_does_not_bootstrap_provider_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "capital.db"
            with ProgramRepository(database) as programs:
                actors = ActorRepository(programs)
                actors.register(Actor("a-1", 0, "worker", "legacy-binding"))

            with LocalActorProviderOperator.open(database) as actor_provider:
                view = actor_provider.show("a-1")
                self.assertFalse(view["configured"])
                self.assertIsNone(view["provider"])
                row = actor_provider._programs._db.execute(
                    """
                    SELECT 1 FROM component_schema
                    WHERE component = 'product_provider_configuration'
                    """,
                ).fetchone()
                self.assertIsNone(row)


if __name__ == "__main__":
    unittest.main()
