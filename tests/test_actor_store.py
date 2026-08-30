from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_capital.kernel.actor_store import ActorRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.errors import IntegrityViolation, PersistenceConflict, StaleActorGeneration
from ai_capital.kernel.models import Actor


class ActorStoreTests(unittest.TestCase):
    def test_actor_identity_survives_model_replacement_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kernel.db"
            with ProgramRepository(path) as host_store:
                actors = ActorRepository(host_store)
                original = actors.register(
                    Actor(
                        "actor-1",
                        0,
                        "bounded worker",
                        "binding-a",
                        grant_refs=("grant-1",),
                    )
                )
                replaced = actors.replace_binding(
                    "actor-1",
                    "binding-b",
                    expected_generation=0,
                )
                self.assertEqual(replaced.actor_id, original.actor_id)
                self.assertEqual(replaced.generation, 1)
                self.assertEqual(replaced.grant_refs, original.grant_refs)
                self.assertEqual(
                    tuple(actor.model_binding for actor in actors.generations("actor-1")),
                    ("binding-a", "binding-b"),
                )

            with ProgramRepository(path) as restarted_store:
                actors = ActorRepository(restarted_store)
                restored = actors.get("actor-1")
                self.assertEqual(restored.actor_id, "actor-1")
                self.assertEqual(restored.generation, 1)
                self.assertEqual(restored.model_binding, "binding-b")
                self.assertEqual(restored.grant_refs, ("grant-1",))

    def test_stale_actor_generation_replacement_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kernel.db"
            with ProgramRepository(path) as host_store:
                actors = ActorRepository(host_store)
                actors.register(Actor("actor-1", 0, "worker", "binding-a"))
                actors.replace_binding("actor-1", "binding-b", expected_generation=0)
                with self.assertRaises(StaleActorGeneration):
                    actors.replace_binding("actor-1", "binding-c", expected_generation=0)

    def test_duplicate_actor_identity_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kernel.db"
            with ProgramRepository(path) as host_store:
                actors = ActorRepository(host_store)
                actors.register(Actor("actor-1", 0, "worker", "binding-a"))
                with self.assertRaises(PersistenceConflict):
                    actors.register(Actor("actor-1", 0, "replacement", "binding-b"))

    def test_actor_projection_corruption_is_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kernel.db"
            with ProgramRepository(path) as host_store:
                actors = ActorRepository(host_store)
                actors.register(Actor("actor-1", 0, "worker", "binding-a"))

            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "UPDATE actor_projections SET actor_json = ? WHERE actor_id = ?",
                    ('{"corrupt":true}', "actor-1"),
                )
                connection.commit()
            finally:
                connection.close()

            with ProgramRepository(path) as host_store:
                actors = ActorRepository(host_store)
                with self.assertRaises(IntegrityViolation):
                    actors.get("actor-1")

    def test_newer_actor_component_schema_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kernel.db"
            with ProgramRepository(path) as host_store:
                ActorRepository(host_store)
                host_store._db.execute(
                    "UPDATE component_schema SET version = 99 WHERE component = ?",
                    ("actor_inference",),
                )
                with self.assertRaises(IntegrityViolation):
                    ActorRepository(host_store)


if __name__ == "__main__":
    unittest.main()
