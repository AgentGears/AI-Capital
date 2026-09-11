from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from ai_capital.kernel.actor_store import ActorRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.models import Actor


ROOT = Path(__file__).resolve().parents[1]


def _run(database: Path, *args: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    current = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = str(ROOT / "src") + (
        os.pathsep + current if current else ""
    )
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "ai_capital.cli",
            "--database",
            str(database),
            *args,
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


class H2ProviderCliTests(unittest.TestCase):
    def test_cli_provider_configuration_and_actor_rebind_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "capital.db"
            with ProgramRepository(database) as programs:
                actors = ActorRepository(programs)
                actors.register(Actor("a-1", 0, "worker", "legacy-binding"))

            registered = _run(
                database,
                "provider-register",
                "--binding-id",
                "binding-a",
                "--adapter",
                "adapter-a",
                "--model",
                "model-a",
                "--settings-json",
                '{"temperature":0.1}',
            )
            self.assertEqual(registered.returncode, 0, registered.stderr)
            registered_json = json.loads(registered.stdout)
            self.assertEqual(registered_json["binding_id"], "binding-a")
            self.assertEqual(registered_json["revision"], 0)

            listed = _run(database, "providers")
            self.assertEqual(listed.returncode, 0, listed.stderr)
            self.assertEqual(json.loads(listed.stdout), [registered_json])

            updated = _run(
                database,
                "provider-update",
                "binding-a",
                "--expected-revision",
                "0",
                "--adapter",
                "adapter-b",
                "--model",
                "model-b",
                "--settings-json",
                '{"reasoning_effort":"medium"}',
            )
            self.assertEqual(updated.returncode, 0, updated.stderr)
            updated_json = json.loads(updated.stdout)
            self.assertEqual(updated_json["revision"], 1)

            history = _run(database, "provider-history", "binding-a")
            self.assertEqual(history.returncode, 0, history.stderr)
            self.assertEqual(
                [item["revision"] for item in json.loads(history.stdout)],
                [0, 1],
            )

            before = _run(database, "actor-provider", "a-1")
            self.assertEqual(before.returncode, 0, before.stderr)
            self.assertFalse(json.loads(before.stdout)["configured"])

            rebound = _run(
                database,
                "actor-rebind",
                "a-1",
                "binding-a",
                "--expected-generation",
                "0",
                "--expected-provider-revision",
                "1",
            )
            self.assertEqual(rebound.returncode, 0, rebound.stderr)
            rebound_json = json.loads(rebound.stdout)
            self.assertEqual(rebound_json["actor"]["actor_id"], "a-1")
            self.assertEqual(rebound_json["actor"]["generation"], 1)
            self.assertEqual(rebound_json["actor"]["model_binding"], "binding-a")
            self.assertEqual(rebound_json["provider"], updated_json)

    def test_cli_rejects_malformed_or_non_object_provider_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "capital.db"
            malformed = _run(
                database,
                "provider-register",
                "--binding-id",
                "binding-a",
                "--adapter",
                "adapter",
                "--model",
                "model",
                "--settings-json",
                "{bad-json",
            )
            self.assertEqual(malformed.returncode, 2)
            self.assertEqual(
                json.loads(malformed.stderr)["error"]["code"],
                "InvalidRequest",
            )

            scalar = _run(
                database,
                "provider-register",
                "--binding-id",
                "binding-a",
                "--adapter",
                "adapter",
                "--model",
                "model",
                "--settings-json",
                "[]",
            )
            self.assertEqual(scalar.returncode, 2)
            self.assertEqual(
                json.loads(scalar.stderr)["error"]["code"],
                "InvalidRequest",
            )

    def test_cli_stale_provider_revision_does_not_replace_actor(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "capital.db"
            with ProgramRepository(database) as programs:
                actors = ActorRepository(programs)
                actors.register(Actor("a-1", 0, "worker", "legacy-binding"))
            self.assertEqual(
                _run(
                    database,
                    "provider-register",
                    "--binding-id",
                    "binding-a",
                    "--adapter",
                    "adapter",
                    "--model",
                    "model",
                ).returncode,
                0,
            )
            stale = _run(
                database,
                "actor-rebind",
                "a-1",
                "binding-a",
                "--expected-generation",
                "0",
                "--expected-provider-revision",
                "4",
            )
            self.assertEqual(stale.returncode, 2)
            self.assertEqual(
                json.loads(stale.stderr)["error"]["code"],
                "StaleProviderConfigurationRevision",
            )
            inspected = _run(database, "actor-provider", "a-1")
            self.assertEqual(inspected.returncode, 0, inspected.stderr)
            actor = json.loads(inspected.stdout)["actor"]
            self.assertEqual(actor["generation"], 0)
            self.assertEqual(actor["model_binding"], "legacy-binding")


if __name__ == "__main__":
    unittest.main()
