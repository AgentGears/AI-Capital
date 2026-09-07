from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_capital.kernel.capability_store import CapabilityRepository, capability_descriptor
from ai_capital.kernel.context import ContextCompiler, ContextRepository
from ai_capital.kernel.durable_program import ProgramRepository
from ai_capital.kernel.enums import ContextCompleteness, ContextPriority, EffectClass, Reversibility, RiskClass
from ai_capital.kernel.errors import ContextBudgetExceeded, IntegrityViolation
from ai_capital.kernel.models import Capability, Program


class K8ReviewRound18Tests(unittest.TestCase):
    def test_hidden_capability_binding_storage_is_rejected_before_snapshot_decode(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "bounded Capability binding"))
                capabilities = CapabilityRepository(programs)
                capability = capabilities.register(Capability(
                    capability_id="capability.hidden-large-binding", schema_version=1,
                    operation="observe", resource_type="artifact",
                    effect_class=EffectClass.OBSERVE, reversibility=Reversibility.REVERSIBLE,
                    risk_class=RiskClass.LOW,
                    input_schema={"type":"object","properties":{},"required":(),"additional_properties":False},
                    output_schema={"type":"object","properties":{},"required":(),"additional_properties":False},
                    binding_revision=0, handler_binding="handler.binding"))
                snapshot = capabilities.create_snapshot((capability_descriptor(capability),))
                programs._db.execute(
                    """
                    UPDATE capability_bindings
                    SET capability_json = capability_json || ?
                    WHERE capability_id = ? AND binding_revision = ?
                    """,
                    (" " * 131072, capability.capability_id, capability.binding_revision),
                )
                contexts = ContextRepository(programs)
                compiler = ContextCompiler(contexts, capabilities=capabilities)
                with patch.object(capabilities, "get_snapshot", side_effect=AssertionError("binding decoded")) as get_snapshot:
                    with self.assertRaises(IntegrityViolation):
                        compiler.compile(program.program_id, budget_units=100000, capability_snapshot=snapshot)
                get_snapshot.assert_not_called()
            finally:
                programs.close()

    def test_current_program_event_storage_is_rejected_before_event_decode(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "small Program"))
                contexts = ContextRepository(programs)
                row = programs._db.execute("SELECT last_sequence FROM program_projections WHERE program_id = ?", (program.program_id,)).fetchone()
                programs._db.execute("UPDATE events SET event_json = event_json || ? WHERE sequence = ?", (" " * 131072, int(row["last_sequence"])))
                with patch.object(contexts, "_decode_event_row", side_effect=AssertionError("Event decoded")) as decode:
                    with self.assertRaises(IntegrityViolation):
                        ContextCompiler(contexts).compile(program.program_id, budget_units=100000)
                decode.assert_not_called()
            finally:
                programs.close()

    def test_persisted_source_event_storage_is_rejected_before_compile_decode(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "persisted source event bound"))
                contexts = ContextRepository(programs)
                ref = contexts.persist_source(program.program_id, priority=ContextPriority.ADVISORY_MEMORY, payload={"memory":"small"})
                programs._db.execute("UPDATE events SET event_json = event_json || ? WHERE event_id = ?", (" " * 131072, ref.removeprefix("event:")))
                with patch.object(contexts, "_event_by_id", side_effect=AssertionError("Event decoded")) as event_by_id:
                    with self.assertRaises(IntegrityViolation):
                        ContextCompiler(contexts).compile(program.program_id, budget_units=100000, source_refs=(ref,))
                event_by_id.assert_not_called()
            finally:
                programs.close()

    def test_persisted_source_event_storage_is_rejected_before_recall_truncation(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "persisted recall event bound"))
                contexts = ContextRepository(programs)
                refs = tuple(contexts.persist_source(program.program_id, priority=ContextPriority.ADVISORY_MEMORY, payload={"n":n}) for n in range(2))
                corrupt = max(refs)
                programs._db.execute("UPDATE events SET event_json = event_json || ? WHERE event_id = ?", (" " * 131072, corrupt.removeprefix("event:")))
                with patch.object(contexts, "_event_by_id", side_effect=AssertionError("Event decoded")) as event_by_id:
                    with self.assertRaises(IntegrityViolation):
                        contexts.recall(program.program_id, refs, max_items=1, max_units=100000)
                event_by_id.assert_not_called()
            finally:
                programs.close()

    def test_current_host_control_is_derived_even_when_caller_omits_it(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "derived Host control coverage"))
                contexts = ContextRepository(programs)
                ref = contexts.persist_source(program.program_id, priority=ContextPriority.HOST_CONTROL, payload={"control":"required"})
                compiled = ContextCompiler(contexts).compile(program.program_id, budget_units=100000)
                self.assertIn(ref, compiled.receipt.included_refs)
                self.assertIs(compiled.receipt.completeness, ContextCompleteness.COMPLETE)
            finally:
                programs.close()

    def test_omitted_current_host_control_cannot_bypass_mandatory_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            programs = ProgramRepository(Path(directory) / "host.db")
            try:
                program = programs.create(Program("p-1", 0, "Host control budget coverage"))
                contexts = ContextRepository(programs)
                compiler = ContextCompiler(contexts)
                baseline = compiler.compile(program.program_id, budget_units=100000)
                contexts.persist_source(program.program_id, priority=ContextPriority.HOST_CONTROL, payload={"control":"x" * 4096})
                with self.assertRaises(ContextBudgetExceeded):
                    compiler.compile(program.program_id, budget_units=baseline.used_units)
            finally:
                programs.close()


if __name__ == "__main__":
    unittest.main()
