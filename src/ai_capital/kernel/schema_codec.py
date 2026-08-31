from __future__ import annotations

import json
import math
import types
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Union, get_args, get_origin, get_type_hints

from .frozen_json import FrozenMap
from .serialization import canonical_json


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate canonical object key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError(f"non-finite canonical number: {value}")


def _decode(annotation: object, value: object) -> object:
    origin = get_origin(annotation)
    args = get_args(annotation)

    if origin is tuple:
        if not isinstance(value, list):
            raise TypeError("canonical tuple must decode from an array")
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_decode(args[0], item) for item in value)
        if len(args) != len(value):
            raise TypeError("fixed canonical tuple has wrong length")
        return tuple(_decode(item_type, item) for item_type, item in zip(args, value))

    if origin in {types.UnionType, Union}:
        if value is None and type(None) in args:
            return None
        failures: list[Exception] = []
        for option in args:
            if option is type(None):
                continue
            try:
                return _decode(option, value)
            except (TypeError, ValueError) as exc:
                failures.append(exc)
        raise TypeError("canonical union value matches no permitted type") from (
            failures[-1] if failures else None
        )

    if annotation is FrozenMap:
        if not isinstance(value, dict):
            raise TypeError("canonical object must decode from an object")
        return FrozenMap(value)

    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return annotation(value)

    if isinstance(annotation, type) and is_dataclass(annotation):
        if not isinstance(value, dict):
            raise TypeError(f"{annotation.__name__} must decode from an object")
        record_fields = fields(annotation)
        expected_names = {field.name for field in record_fields}
        actual_names = set(value)
        if actual_names != expected_names:
            missing = sorted(expected_names - actual_names)
            unknown = sorted(actual_names - expected_names)
            raise TypeError(
                f"{annotation.__name__} schema mismatch; missing={missing}, unknown={unknown}"
            )
        hints = get_type_hints(annotation)
        return annotation(**{
            field.name: _decode(hints[field.name], value[field.name])
            for field in record_fields
        })

    if annotation is str:
        if type(value) is not str:
            raise TypeError("expected string")
        return value
    if annotation is bool:
        if type(value) is not bool:
            raise TypeError("expected boolean")
        return value
    if annotation is int:
        if type(value) is not int:
            raise TypeError("expected integer")
        return value
    if annotation is float:
        if type(value) not in {int, float} or type(value) is bool:
            raise TypeError("expected number")
        decoded = float(value)
        if not math.isfinite(decoded):
            raise ValueError("canonical number must be finite")
        return decoded
    if annotation is type(None):
        if value is not None:
            raise TypeError("expected null")
        return None

    raise TypeError(f"unsupported canonical annotation: {annotation!r}")


def record_to_json(record: object) -> str:
    if not is_dataclass(record) or isinstance(record, type):
        raise TypeError("record_to_json requires a dataclass instance")
    return canonical_json(record)


def record_from_json(record_type: type, payload: str) -> object:
    if not isinstance(record_type, type) or not is_dataclass(record_type):
        raise TypeError("record_from_json requires a dataclass type")
    data = json.loads(
        payload,
        object_pairs_hook=_strict_object,
        parse_constant=_reject_constant,
    )
    return _decode(record_type, data)
