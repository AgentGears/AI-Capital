from __future__ import annotations

import math
from collections.abc import Iterator, Mapping


_JSON_SCALARS = (str, int, bool, type(None))


class FrozenMap(Mapping[str, object]):
    """Small immutable mapping for canonical JSON-shaped kernel values."""

    __slots__ = ("_items",)

    def __init__(self, source: Mapping[str, object] | None = None):
        source = source or {}
        self._items = tuple(
            sorted(
                ((str(key), freeze_json(value)) for key, value in source.items()),
                key=lambda item: item[0],
            )
        )

    def __getitem__(self, key: str) -> object:
        for item_key, value in self._items:
            if item_key == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return (key for key, _ in self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __hash__(self) -> int:
        return hash(self._items)

    def items_tuple(self) -> tuple[tuple[str, object], ...]:
        return self._items


def freeze_json(value: object) -> object:
    if isinstance(value, FrozenMap):
        return value
    if isinstance(value, Mapping):
        return FrozenMap(value)
    if isinstance(value, (tuple, list)):
        return tuple(freeze_json(item) for item in value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical numeric values must be finite")
        return value
    if isinstance(value, _JSON_SCALARS):
        return value
    raise TypeError(f"unsupported canonical JSON value: {type(value)!r}")
