from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_capital.kernel.capability_store import CapabilityRepository, capability_descriptor
from ai_capital.kernel.context import ContextCompiler, ContextRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ContextPriority, EffectClass, Reversibility, RiskClass
from ai_capital.kernel.errors import ContextBudgetExceeded
from ai_capital.kernel.models import Capability, Program


class K8ReviewRound28Tests(unittest.TestCase):
    def test_persisted_recall_materializes_payload_projection_not_backing_event(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program_id = "p-" + ("x" * 65536)
                program = programs.create(
                    Program(program_id, 0, "bounded projected persisted-source recall")
                )
                contexts = ContextRepository(programs)
                source_ref = contexts.persist_source(
                    program.program_id,
                    priority=ContextPriority.ADVISORY_MEMORY,
                    payload={"note": "tiny"},
                )
                preflight = contexts._persisted_source_preflight(
                    program.program_id, source_ref
                )
                self.assertGreater(preflight.event_units, 100_000)

                with patch.object(
                    contexts,
                    "_event_by_id",
                    side_effect=AssertionError("persisted backing Event was decoded"),
                ) as event_by_id:
                    recalled = contexts.recall(
                        program.program_id,
                        (source_ref,),
                        max_items=1,
                        max_units=2048,
                    )

                event_by_id.assert_not_called()
                self.assertEqual(recalled.included_refs, (source_ref,))
                self.assertEqual(recalled.excluded_refs, ())
                self.assertIs(recalled.items[0].priority, ContextPriority.RECALLED_HISTORY)
                self.assertEqual(dict(recalled.items[0].payload), {"note": "tiny"})
            finally:
                programs.close()

    def test_v4_persisted_projection_backfills_payload_on_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "v4 payload projection migration"))
                contexts = ContextRepository(programs)
                source_ref = contexts.persist_source(
                    program.program_id,
                    priority=ContextPriority.RECENT_INTERACTION,
                    payload={"turn": "durable"},
                )
                with programs._transaction():
                    programs._db.execute(
                        "DROP INDEX IF EXISTS context_persisted_source_program_priority"
                    )
                    programs._db.execute(
                        "ALTER TABLE context_persisted_source_index RENAME TO context_persisted_source_index_v5"
                    )
                    programs._db.execute(
                        """
                        CREATE TABLE context_persisted_source_index (
                            sequence INTEGER PRIMARY KEY,
                            event_id TEXT NOT NULL UNIQUE,
                            program_id TEXT NOT NULL,
                            program_revision INTEGER NOT NULL,
                            priority TEXT NOT NULL,
                            source_digest TEXT NOT NULL,
                            payload_units INTEGER NOT NULL,
                            event_digest TEXT NOT NULL,
                            projection_digest TEXT NOT NULL,
                            FOREIGN KEY(sequence) REFERENCES events(sequence)
                        )
                        """
                    )
                    programs._db.execute(
                        """
                        INSERT INTO context_persisted_source_index(
                            sequence, event_id, program_id, program_revision, priority,
                            source_digest, payload_units, event_digest, projection_digest
                        )
                        SELECT sequence, event_id, program_id, program_revision, priority,
                               source_digest, payload_units, event_digest, projection_digest
                        FROM context_persisted_source_index_v5
                        """
                    )
                    programs._db.execute(
                        "DROP TABLE context_persisted_source_index_v5"
                    )
                    programs._db.execute(
                        "UPDATE component_schema SET version = 4 WHERE component = 'bounded_context'"
                    )

                migrated = ContextRepository(programs)
                version = programs._db.execute(
                    "SELECT version FROM component_schema WHERE component = 'bounded_context'"
                ).fetchone()[0]
                payload_units = programs._db.execute(
                    "SELECT length(CAST(payload_json AS BLOB)) FROM context_persisted_source_index WHERE event_id = ?",
                    (source_ref.removeprefix("event:"),),
                ).fetchone()[0]
                self.assertEqual(int(version), 10)
                self.assertGreater(int(payload_units), 0)

                with patch.object(
                    migrated,
                    "_event_by_id",
                    side_effect=AssertionError("migrated persisted Event was decoded"),
                ):
                    recalled = migrated.recall(
                        program.program_id,
                        (source_ref,),
                        max_items=1,
                        max_units=2048,
                    )
                self.assertEqual(recalled.included_refs, (source_ref,))
            finally:
                programs.close()

    def _oversized_snapshot(self, programs: ProgramRepository):
        capabilities = CapabilityRepository(programs)
        capability = capabilities.register(
            Capability(
                capability_id="capability.round28-large",
                schema_version=1,
                operation="observe",
                resource_type="artifact",
                effect_class=EffectClass.OBSERVE,
                reversibility=Reversibility.REVERSIBLE,
                risk_class=RiskClass.LOW,
                input_schema={
                    "type": "object",
                    "properties": {"x" * 131072: {"type": "string"}},
                    "required": (),
                    "additional_properties": False,
                },
                output_schema={
                    "type": "object",
                    "properties": {},
                    "required": (),
                    "additional_properties": False,
                },
                binding_revision=0,
                handler_binding="handler.round28",
            )
        )
        return capabilities, capabilities.create_snapshot((capability_descriptor(capability),))

    def test_oversized_capability_snapshot_fails_before_binding_metadata_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "Capability budget before binding scan"))
                capabilities, snapshot = self._oversized_snapshot(programs)
                contexts = ContextRepository(programs)
                baseline = ContextCompiler(contexts).compile(
                    program.program_id,
                    budget_units=100_000,
                )
                compiler = ContextCompiler(contexts, capabilities=capabilities)
                with patch.object(
                    capabilities,
                    "_snapshot_binding_units",
                    side_effect=AssertionError("oversized snapshot scanned historical bindings"),
                ) as binding_scan:
                    with self.assertRaises(ContextBudgetExceeded):
                        compiler.compile(
                            program.program_id,
                            budget_units=baseline.used_units,
                            capability_snapshot=snapshot,
                        )
                binding_scan.assert_not_called()
            finally:
                programs.close()

    def test_fitting_capability_snapshot_scans_bindings_after_budget_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "Capability bounded binding verification"))
                capabilities = CapabilityRepository(programs)
                capability = capabilities.register(
                    Capability(
                        capability_id="capability.round28-small",
                        schema_version=1,
                        operation="observe",
                        resource_type="artifact",
                        effect_class=EffectClass.OBSERVE,
                        reversibility=Reversibility.REVERSIBLE,
                        risk_class=RiskClass.LOW,
                        input_schema={"type": "object", "properties": {}, "required": (), "additional_properties": False},
                        output_schema={"type": "object", "properties": {}, "required": (), "additional_properties": False},
                        binding_revision=0,
                        handler_binding="handler.round28-small",
                    )
                )
                snapshot = capabilities.create_snapshot((capability_descriptor(capability),))
                contexts = ContextRepository(programs)
                compiler = ContextCompiler(contexts, capabilities=capabilities)
                with patch.object(
                    capabilities,
                    "_snapshot_binding_units",
                    wraps=capabilities._snapshot_binding_units,
                ) as binding_scan:
                    compiler.compile(
                        program.program_id,
                        budget_units=100_000,
                        capability_snapshot=snapshot,
                    )
                binding_scan.assert_called_once_with(snapshot.capabilities)
            finally:
                programs.close()


if __name__ == "__main__":
    unittest.main()
