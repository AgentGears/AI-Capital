from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_capital.kernel.errors import InvalidRequest
from ai_capital.kernel.frozen_json import FrozenMap
from ai_capital.kernel.structured_schema import (
    validate_schema_definition,
    validate_structured_value,
)


class StructuredSchemaTests(unittest.TestCase):
    def test_valid_object_schema_normalizes_value(self):
        schema = {
            "type": "object",
            "properties": {
                "path": {"type": "string", "min_length": 1},
                "count": {"type": "integer", "minimum": 0},
            },
            "required": ("path",),
            "additional_properties": False,
        }
        normalized = validate_structured_value(
            schema,
            {"path": "a.txt", "count": 2},
        )
        self.assertIsInstance(normalized, FrozenMap)
        self.assertEqual(normalized["path"], "a.txt")
        self.assertEqual(normalized["count"], 2)

    def test_unknown_fields_fail_closed(self):
        schema = {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ("path",),
            "additional_properties": False,
        }
        with self.assertRaises(InvalidRequest):
            validate_structured_value(schema, {"path": "a", "extra": True})

    def test_required_field_must_exist(self):
        schema = {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ("path",),
            "additional_properties": False,
        }
        with self.assertRaises(InvalidRequest):
            validate_structured_value(schema, {})

    def test_schema_definition_rejects_unknown_keywords(self):
        with self.assertRaises(InvalidRequest):
            validate_schema_definition({"type": "string", "pattern": ".*"})

    def test_required_cannot_reference_undefined_property(self):
        with self.assertRaises(InvalidRequest):
            validate_schema_definition({
                "type": "object",
                "properties": {},
                "required": ("missing",),
                "additional_properties": False,
            })

    def test_array_item_validation_is_recursive(self):
        schema = {
            "type": "array",
            "items": {"type": "integer", "minimum": 0},
            "min_items": 1,
            "max_items": 3,
        }
        self.assertEqual(validate_structured_value(schema, [1, 2]), (1, 2))
        with self.assertRaises(InvalidRequest):
            validate_structured_value(schema, [1, -1])

    def test_boolean_is_not_accepted_as_integer_or_number(self):
        with self.assertRaises(InvalidRequest):
            validate_structured_value({"type": "integer"}, True)
        with self.assertRaises(InvalidRequest):
            validate_structured_value({"type": "number"}, False)

    def test_string_enum_and_bounds(self):
        schema = {
            "type": "string",
            "enum": ("read", "write"),
            "min_length": 4,
            "max_length": 5,
        }
        self.assertEqual(validate_structured_value(schema, "read"), "read")
        with self.assertRaises(InvalidRequest):
            validate_structured_value(schema, "other")


if __name__ == "__main__":
    unittest.main()
