from __future__ import annotations

from pathlib import Path


test_path = Path("tests/test_k8_review_round38.py")
test_text = test_path.read_text(encoding="utf-8")
needle = '            source["priority"] = ContextPriority.ADVISORY_MEMORY.value\n'
replacement = (
    needle
    + '            source["currentness"] = "advisory"\n'
    + '            source["authority"] = "advisory"\n'
)
if test_text.count(needle) != 1:
    raise RuntimeError("Round 38 fixture target not found exactly once")
test_path.write_text(test_text.replace(needle, replacement, 1), encoding="utf-8")

context_path = Path("src/ai_capital/kernel/context.py")
context = context_path.read_text(encoding="utf-8")
old = """                      AND OLD.context_source_metadata_digest IS NOT NULL
                      AND (
                          OLD.event_type IS NOT NEW.event_type
                          OR OLD.event_json IS NOT NEW.event_json
                          OR OLD.event_digest IS NOT NEW.event_digest
                          OR OLD.context_source_program_id IS NOT NEW.context_source_program_id
                          OR OLD.context_source_program_revision IS NOT NEW.context_source_program_revision
                          OR OLD.context_source_priority IS NOT NEW.context_source_priority
                          OR OLD.context_source_metadata_digest IS NOT NEW.context_source_metadata_digest
                      );

                    DELETE FROM context_persisted_source_index
"""
new = """                      AND OLD.context_source_metadata_digest IS NOT NULL
                      AND NOT (
                          OLD.event_type IS NEW.event_type
                          AND OLD.event_json IS NEW.event_json
                          AND OLD.event_digest IS NEW.event_digest
                          AND NEW.context_source_program_id IS NULL
                          AND NEW.context_source_program_revision IS NULL
                          AND NEW.context_source_priority IS NULL
                          AND NEW.context_source_metadata_digest IS NULL
                      )
                      AND (
                          OLD.event_type IS NOT NEW.event_type
                          OR OLD.event_json IS NOT NEW.event_json
                          OR OLD.event_digest IS NOT NEW.event_digest
                          OR OLD.context_source_program_id IS NOT NEW.context_source_program_id
                          OR OLD.context_source_program_revision IS NOT NEW.context_source_program_revision
                          OR OLD.context_source_priority IS NOT NEW.context_source_priority
                          OR OLD.context_source_metadata_digest IS NOT NEW.context_source_metadata_digest
                      );

                    UPDATE events
                    SET context_source_program_id = (
                            SELECT program_id FROM context_persisted_source_index
                            WHERE event_id = OLD.event_id
                        ),
                        context_source_program_revision = (
                            SELECT program_revision FROM context_persisted_source_index
                            WHERE event_id = OLD.event_id
                        ),
                        context_source_priority = (
                            SELECT priority FROM context_persisted_source_index
                            WHERE event_id = OLD.event_id
                        ),
                        context_source_metadata_digest = OLD.context_source_metadata_digest
                    WHERE sequence = OLD.sequence
                      AND OLD.event_type = 'context.source_persisted'
                      AND NEW.event_type = 'context.source_persisted'
                      AND OLD.event_json IS NEW.event_json
                      AND OLD.event_digest IS NEW.event_digest
                      AND NEW.context_source_program_id IS NULL
                      AND NEW.context_source_program_revision IS NULL
                      AND NEW.context_source_priority IS NULL
                      AND NEW.context_source_metadata_digest IS NULL
                      AND EXISTS (
                          SELECT 1 FROM context_persisted_source_index
                          WHERE event_id = OLD.event_id
                      );

                    DELETE FROM context_persisted_source_index
"""
if context.count(old) != 1:
    raise RuntimeError("Round 38 scalar-backfill compatibility target not found exactly once")
context_path.write_text(context.replace(old, new, 1), encoding="utf-8")
