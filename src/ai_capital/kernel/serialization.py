from __future__ import annotations

import json
from dataclasses import fields, is_dataclass
from enum import Enum
from hashlib import sha256
from typing import Mapping


def to_canonical_data(value: object) -> object:
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: to_canonical_data(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {
            str(key): to_canonical_data(value[key])
            for key in sorted(value, key=lambda item: str(item))
        }
    if isinstance(value, (tuple, list)):
        return [to_canonical_data(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"unsupported canonical value: {type(value)!r}")


def canonical_json(value: object) -> str:
    return json.dumps(
        to_canonical_data(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def canonical_digest(value: object) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()
