"""Frozen dataclasses to JSON and back, so a case's state survives a restart.

Canonical on purpose: keys sorted, sets as sorted lists, Decimal as text (so NaN survives), datetimes as ISO text with their
offset. Loading is strict: a missing or extra key, a naive datetime, a float for money or a value an enum doesn't have is
an error, never a quiet default. The SQLite step and the approval digest reuse this.
"""

from __future__ import annotations

import functools
import json
import types
import typing
from dataclasses import fields, is_dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any


class Unloadable(ValueError):
    """Saved data that doesn't fit the type it should have. The message says where."""


def dump(value: Any) -> Any:
    """value as plain JSON data."""
    kind = type(value)
    if value is None or kind in (str, int, bool):
        return value
    if kind is Decimal:
        return str(value)
    if kind is tuple:
        return [dump(item) for item in value]
    names = _names(kind)
    if names is not None:
        return {name: dump(getattr(value, name)) for name in names}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        if value.utcoffset() is None:
            raise ValueError(f"{value} has no time zone, so it can't be saved")
        return value.isoformat()
    if isinstance(value, frozenset):
        items = [dump(item) for item in value]
        return sorted(items) if all(type(item) is str for item in items) else sorted(items, key=_text)
    raise TypeError(f"a {kind.__name__} isn't saved")


def canonical(value: Any) -> str:
    """One text per value. Records are compared by it, because Decimal NaN never equals itself."""
    return _text(dump(value))


def load(kind: Any, data: Any, where: str = "the record") -> Any:
    """data, as dump() wrote it, back as kind."""
    origin, args = _shape(kind)
    if origin in (typing.Union, types.UnionType):
        if data is None and type(None) in args:
            return None
        rest = [arg for arg in args if arg is not type(None)]
        if len(rest) != 1:
            raise TypeError(f"{kind} isn't saved")
        return load(rest[0], data, where)
    if origin is tuple:
        if len(args) != 2 or args[1] is not Ellipsis:
            raise TypeError(f"{kind} isn't saved")
        return tuple(load(args[0], item, f"{where}[{number}]") for number, item in enumerate(_list(data, where)))
    if origin is frozenset:
        items = [load(args[0], item, f"{where}[{number}]") for number, item in enumerate(_list(data, where))]
        if len(set(items)) != len(items):
            raise Unloadable(f"{where} lists a value twice.")
        return frozenset(items)
    names = _names(kind)
    if names is not None:
        if not isinstance(data, dict):
            raise Unloadable(f"{where} isn't an object.")
        if len(data) != len(names) or any(name not in data for name in names):
            missing, extra = sorted(set(names) - set(data)), sorted(set(data) - set(names))
            raise Unloadable(f"{where} doesn't fit {kind.__name__}: missing {missing}, not expected {extra}.")
        hints = _hints(kind)
        return kind(**{name: load(hints[name], data[name], f"{where}.{name}") for name in names})
    if isinstance(kind, type) and issubclass(kind, Enum):
        members = _members(kind)
        if type(data) is not str or data not in members:  # Stage and Status would turn any text into UNKNOWN
            raise Unloadable(f"{where} is {data!r}, which isn't a {kind.__name__}.")
        return members[data]
    if kind is Decimal:
        try:
            value = Decimal(_typed(data, str, where))
        except InvalidOperation:
            raise Unloadable(f"{where} is {data!r}, which isn't a number.") from None
        if value.is_snan():  # it raises on any sum or comparison, and dump() never writes one
            raise Unloadable(f"{where} is {data!r}, which isn't a number.")
        return value
    if kind is datetime:
        try:
            value = datetime.fromisoformat(_typed(data, str, where))
        except ValueError:
            raise Unloadable(f"{where} is {data!r}, which isn't a date and time.") from None
        if value.utcoffset() is None:
            raise Unloadable(f"{where} has no time zone.")
        return value
    if kind in (str, int, bool):
        return _typed(data, kind, where)
    raise TypeError(f"{kind} isn't saved")


# Looked up once per type: a case record is saved and compared on every reading.
@functools.cache
def _names(kind: type) -> tuple[str, ...] | None:
    return tuple(item.name for item in fields(kind)) if is_dataclass(kind) else None


@functools.cache
def _hints(kind: type) -> dict[str, Any]:
    return typing.get_type_hints(kind)


@functools.cache
def _shape(kind: Any) -> tuple[Any, tuple[Any, ...]]:
    return typing.get_origin(kind), typing.get_args(kind)


@functools.cache
def _members(kind: type[Enum]) -> dict[Any, Enum]:
    return {member.value: member for member in kind}


def _text(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _list(data: Any, where: str) -> list[Any]:
    if not isinstance(data, list):
        raise Unloadable(f"{where} isn't a list.")
    return data


def _typed(data: Any, kind: type, where: str) -> Any:
    if type(data) is not kind:  # exact, so True never passes for 1 and 1 never for "1"
        raise Unloadable(f"{where} is {data!r}, not a {kind.__name__}.")
    return data
