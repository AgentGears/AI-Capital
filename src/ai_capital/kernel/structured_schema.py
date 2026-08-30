from __future__ import annotations

from collections.abc import Mapping

from .errors import InvalidRequest
from .frozen_json import FrozenMap, freeze_json


_ALLOWED_TYPES = frozenset({"object", "string", "integer", "number", "boolean", "array"})
_COMMON_KEYS = frozenset({"type"})
_TYPE_KEYS = {
    "object": frozenset({"properties", "required", "additional_properties"}),
    "string": frozenset({"enum", "min_length", "max_length"}),
    "integer": frozenset({"minimum", "maximum"}),
    "number": frozenset({"minimum", "maximum"}),
    "boolean": frozenset(),
    "array": frozenset({"items", "min_items", "max_items"}),
}


def _object(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise InvalidRequest(f"{label} must be an object")
    return value


def _integer(value: object, label: str) -> int:
    if type(value) is not int:
        raise InvalidRequest(f"{label} must be an integer")
    return value


def validate_schema_definition(schema: object, *, path: str = "$schema") -> FrozenMap:
    frozen = freeze_json(schema)
    if not isinstance(frozen, FrozenMap):
        raise InvalidRequest(f"{path} must be an object")

    type_name = frozen.get("type")
    if type_name not in _ALLOWED_TYPES:
        raise InvalidRequest(f"{path}.type is unsupported: {type_name!r}")

    allowed = _COMMON_KEYS | _TYPE_KEYS[type_name]
    unknown = set(frozen) - allowed
    if unknown:
        raise InvalidRequest(f"{path} has unsupported keys: {sorted(unknown)}")

    if type_name == "object":
        properties = frozen.get("properties", FrozenMap())
        properties = _object(properties, f"{path}.properties")
        for name, child in properties.items():
            if not isinstance(name, str) or not name:
                raise InvalidRequest(f"{path}.properties keys must be non-empty strings")
            validate_schema_definition(child, path=f"{path}.properties.{name}")

        required = frozen.get("required", ())
        if not isinstance(required, tuple):
            raise InvalidRequest(f"{path}.required must be an array")
        if any(type(name) is not str or not name for name in required):
            raise InvalidRequest(f"{path}.required entries must be non-empty strings")
        if len(set(required)) != len(required):
            raise InvalidRequest(f"{path}.required contains duplicates")
        missing_properties = set(required) - set(properties)
        if missing_properties:
            raise InvalidRequest(
                f"{path}.required references undefined properties: {sorted(missing_properties)}"
            )
        additional = frozen.get("additional_properties", False)
        if type(additional) is not bool:
            raise InvalidRequest(f"{path}.additional_properties must be boolean")

    elif type_name == "string":
        enum = frozen.get("enum")
        if enum is not None:
            if not isinstance(enum, tuple) or not enum:
                raise InvalidRequest(f"{path}.enum must be a non-empty array")
            if any(type(item) is not str for item in enum):
                raise InvalidRequest(f"{path}.enum entries must be strings")
            if len(set(enum)) != len(enum):
                raise InvalidRequest(f"{path}.enum contains duplicates")
        for key in ("min_length", "max_length"):
            if key in frozen and _integer(frozen[key], f"{path}.{key}") < 0:
                raise InvalidRequest(f"{path}.{key} must be non-negative")
        if "min_length" in frozen and "max_length" in frozen:
            if frozen["min_length"] > frozen["max_length"]:
                raise InvalidRequest(f"{path} has min_length greater than max_length")

    elif type_name in {"integer", "number"}:
        for key in ("minimum", "maximum"):
            if key in frozen:
                value = frozen[key]
                if type(value) not in {int, float} or type(value) is bool:
                    raise InvalidRequest(f"{path}.{key} must be numeric")
        if "minimum" in frozen and "maximum" in frozen:
            if frozen["minimum"] > frozen["maximum"]:
                raise InvalidRequest(f"{path} has minimum greater than maximum")

    elif type_name == "array":
        if "items" not in frozen:
            raise InvalidRequest(f"{path}.items is required")
        validate_schema_definition(frozen["items"], path=f"{path}.items")
        for key in ("min_items", "max_items"):
            if key in frozen and _integer(frozen[key], f"{path}.{key}") < 0:
                raise InvalidRequest(f"{path}.{key} must be non-negative")
        if "min_items" in frozen and "max_items" in frozen:
            if frozen["min_items"] > frozen["max_items"]:
                raise InvalidRequest(f"{path} has min_items greater than max_items")

    return frozen


def validate_structured_value(schema: object, value: object, *, path: str = "$") -> object:
    schema = validate_schema_definition(schema)
    type_name = schema["type"]

    if type_name == "object":
        if not isinstance(value, Mapping):
            raise InvalidRequest(f"{path} must be an object")
        properties = schema.get("properties", FrozenMap())
        required = schema.get("required", ())
        for name in required:
            if name not in value:
                raise InvalidRequest(f"{path}.{name} is required")
        unknown = set(value) - set(properties)
        if unknown and not schema.get("additional_properties", False):
            raise InvalidRequest(f"{path} has unexpected fields: {sorted(unknown)}")
        for name, child_value in value.items():
            if name in properties:
                validate_structured_value(properties[name], child_value, path=f"{path}.{name}")
        return freeze_json(value)

    if type_name == "string":
        if type(value) is not str:
            raise InvalidRequest(f"{path} must be a string")
        if "enum" in schema and value not in schema["enum"]:
            raise InvalidRequest(f"{path} is not an allowed value")
        if "min_length" in schema and len(value) < schema["min_length"]:
            raise InvalidRequest(f"{path} is shorter than minimum length")
        if "max_length" in schema and len(value) > schema["max_length"]:
            raise InvalidRequest(f"{path} is longer than maximum length")
        return value

    if type_name == "integer":
        if type(value) is not int:
            raise InvalidRequest(f"{path} must be an integer")
        if "minimum" in schema and value < schema["minimum"]:
            raise InvalidRequest(f"{path} is below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise InvalidRequest(f"{path} is above maximum")
        return value

    if type_name == "number":
        if type(value) not in {int, float} or type(value) is bool:
            raise InvalidRequest(f"{path} must be numeric")
        if "minimum" in schema and value < schema["minimum"]:
            raise InvalidRequest(f"{path} is below minimum")
        if "maximum" in schema and value > schema["maximum"]:
            raise InvalidRequest(f"{path} is above maximum")
        return value

    if type_name == "boolean":
        if type(value) is not bool:
            raise InvalidRequest(f"{path} must be boolean")
        return value

    if type_name == "array":
        if not isinstance(value, (tuple, list)):
            raise InvalidRequest(f"{path} must be an array")
        if "min_items" in schema and len(value) < schema["min_items"]:
            raise InvalidRequest(f"{path} has too few items")
        if "max_items" in schema and len(value) > schema["max_items"]:
            raise InvalidRequest(f"{path} has too many items")
        for index, item in enumerate(value):
            validate_structured_value(schema["items"], item, path=f"{path}[{index}]")
        return freeze_json(value)

    raise InvalidRequest(f"{path} uses unsupported schema type {type_name!r}")
