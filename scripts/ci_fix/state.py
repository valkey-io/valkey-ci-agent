"""Carry a fix's state from one workflow job to the next, as JSON.

A detector-issue fix runs as three jobs (plan, fix, publish); each reads what
the one before wrote. The state is plain dataclasses (``FixTask``,
``FixCandidate``, ...), so it is encoded field by field and decoded by the
same type hints: no pickling, and a value that does not fit its field is an
error rather than a surprise.
"""

from __future__ import annotations

import dataclasses
import json
import types
import typing
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar, Union, get_args, get_origin, get_type_hints

T = TypeVar("T")
# ``X | None`` (3.10+) as well as ``Optional[X]``.
_UNIONS = (Union, getattr(types, "UnionType", Union))


def encode(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {field.name: encode(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [encode(item) for item in value]
    return value


def decode(kind: Any, data: Any) -> Any:
    origin = get_origin(kind)
    if origin in _UNIONS:
        options = [option for option in get_args(kind) if option is not type(None)]
        if data is None:
            return None
        return decode(options[0], data)
    if origin is tuple:
        item = get_args(kind)[0]
        return tuple(decode(item, value) for value in data)
    if origin is dict:
        key, item = get_args(kind)
        return {decode(key, name): decode(item, value) for name, value in data.items()}
    if isinstance(kind, type) and dataclasses.is_dataclass(kind):
        hints = get_type_hints(kind)
        return kind(**{
            field.name: decode(hints[field.name], data[field.name])
            for field in dataclasses.fields(kind) if field.name in data
        })
    if isinstance(kind, type) and issubclass(kind, Enum):
        return kind(data)
    if kind in (str, int, bool):
        if not isinstance(data, kind):
            raise TypeError(f"expected {kind.__name__}, got {type(data).__name__}")
        return data
    raise TypeError(f"cannot decode {kind!r}")


def save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(encode(value), indent=2, sort_keys=True), encoding="utf-8")


def load(path: Path, kind: type[T]) -> T:
    return typing.cast(T, decode(kind, json.loads(path.read_text(encoding="utf-8"))))
