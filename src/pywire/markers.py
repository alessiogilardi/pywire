from __future__ import annotations

from typing import Annotated, Any, get_args, get_origin


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


def resolve_autowired_type(
    annotation: Any,  # noqa: ANN401
    module_globals: dict[str, Any],
) -> Any | None:  # noqa: ANN401
    """Return the wrapped type if annotation is Autowired[T], else None.

    If Autowired[T] is unresolved (T is a forward-reference string that
    cannot be evaluated against module_globals), returns None.

    annotation is a raw, unvalidated type-hint object -- it can genuinely be
    anything (a type, a string, an Annotated[...] instance, ...) before this
    function has inspected it, so Any is the honest type here, not a missed
    narrowing opportunity. The same reasoning applies to the return type: it
    passes through whatever was wrapped, unexamined.
    """
    if get_origin(annotation) is not Autowired:
        return None

    (wrapped,) = get_args(annotation)

    # Forward references inside Autowired["X"] are left unresolved by
    # eval_str=True, since it only evaluates the outer annotation string.
    # Under the PEP 695 alias, such a reference surfaces as a plain str
    # rather than a ForwardRef.
    if isinstance(wrapped, str):
        try:
            return eval(wrapped, module_globals)
        except NameError:
            return None

    return wrapped
