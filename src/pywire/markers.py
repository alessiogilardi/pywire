from __future__ import annotations

from typing import Any


class _AutowiredMarker:
    """Marker interno usato per rappresentare Autowired[T]."""

    __slots__ = ("wrapped_type",)

    def __init__(self, wrapped_type: Any) -> None:
        self.wrapped_type = wrapped_type

    def __repr__(self) -> str:
        name = getattr(self.wrapped_type, "__name__", self.wrapped_type)
        return f"Autowired[{name}]"


class Autowired:
    """Marker per dichiarare una dependency injection.

    Example:
        class Repository:
            client: Autowired[DBClient]
    """

    def __class_getitem__(cls, item: Any) -> _AutowiredMarker:
        return _AutowiredMarker(item)
