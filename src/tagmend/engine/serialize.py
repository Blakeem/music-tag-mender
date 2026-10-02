"""The field serializer the engine's result dataclasses share for their MCP payloads."""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from _typeshed import DataclassInstance


class FieldDict:
    """Gives a dataclass a ``to_dict`` that serializes its own fields in field order.

    A class whose payload renames, omits, rounds or adds a key keeps its own ``to_dict``.
    """

    # Empty, so a slots dataclass built on this mixin still carries no instance dict.
    __slots__ = ()

    def to_dict(self: DataclassInstance) -> dict[str, object]:
        """JSON-serializable form for the MCP tools."""
        return _field_dict(self)


def _field_dict(instance: DataclassInstance) -> dict[str, object]:
    """Return *instance*'s fields in field order, each value made JSON-ready."""
    return {entry.name: _plain(getattr(instance, entry.name)) for entry in fields(instance)}


def _plain(value: object) -> object:
    """Return *value* JSON-ready: its own ``to_dict`` first, then a dataclass, list or dict."""
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    if is_dataclass(value) and not isinstance(value, type):
        return _field_dict(value)
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    return value
