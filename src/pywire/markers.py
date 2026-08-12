from __future__ import annotations

from typing import Annotated, TypeVar

T = TypeVar("T")


class _AutowiredMeta:
    """Sentinel tag carried as Annotated metadata to mark injected fields."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "Autowired"


_AUTOWIRED = _AutowiredMeta()

# Built on Annotated so static type checkers see the wrapped type T directly
# instead of an opaque marker, while the container still recovers the
# _AUTOWIRED tag at runtime via typing.get_origin/get_args.
#
# Example:
#     class Repository:
#         client: Autowired[DBClient]
Autowired = Annotated[T, _AUTOWIRED]
