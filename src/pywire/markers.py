from __future__ import annotations

from typing import Annotated


class _AutowiredMeta:
    """Sentinel tag carried as Annotated metadata to mark injected fields."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "Autowired"


_AUTOWIRED = _AutowiredMeta()

# PEP 695 type alias: static type checkers see the wrapped type T directly
# instead of an opaque marker, while the container recovers the _AUTOWIRED
# tag at runtime via typing.get_origin (origin is this alias itself, not
# Annotated) followed by typing.get_args to extract T.
#
# Example:
#     class Repository:
#         client: Autowired[DBClient]
type Autowired[T] = Annotated[T, _AUTOWIRED]
