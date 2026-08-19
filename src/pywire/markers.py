from __future__ import annotations

import builtins
from typing import Annotated, Any, get_args, get_origin, get_type_hints, override

from .exceptions import AnnotationResolutionError


class _AutowiredMeta:
    """Sentinel tag carried as Annotated metadata to mark injected fields."""

    __slots__ = ()

    @override
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


class _MissingName:
    """Placeholder standing in for a name an annotation could not resolve.

    Substituting a placeholder instead of letting eval() raise makes annotation
    evaluation total: every annotation yields a value, and whether the failure
    matters is decided afterwards, in one place, by resolve_autowired_type.
    Attribute access chains, so a dotted reference such as pkg.Thing produces a
    single placeholder that still knows its full name.
    """

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __getattr__(self, attribute: str) -> _MissingName:
        return _MissingName(f"{self.name}.{attribute}")

    @override
    def __repr__(self) -> str:
        return f"<unresolved {self.name}>"


class _MissingNames(dict[str, Any]):
    """eval() locals mapping that manufactures placeholders for unknown names.

    Passed as *locals*, which is consulted before globals -- so it has to
    delegate to the real module globals and then to builtins before inventing
    anything. Without that delegation it would shadow every name in the
    annotation, Autowired included.

    A dict subclass rather than a plain Mapping on purpose: CPython reads an
    exact dict through PyDict_GetItem, which never consults __missing__, but
    reads a subclass through PyObject_GetItem, which does.
    """

    __slots__ = ("_globals",)

    def __init__(self, module_globals: dict[str, Any]) -> None:
        super().__init__()
        self._globals = module_globals

    def __missing__(self, key: str) -> Any:  # noqa: ANN401
        """Resolve key against real globals, then builtins, else placeholder.

        Any is the honest return type: this hands back whatever the module
        happens to have bound to that name.
        """
        if key in self._globals:
            return self._globals[key]

        if hasattr(builtins, key):
            return getattr(builtins, key)

        return _MissingName(key)


def evaluate_annotation(
    annotation: object,
    module_globals: dict[str, Any],
) -> Any:  # noqa: ANN401
    """Evaluate a possibly-stringified annotation. Never raises.

    Unresolvable names become _MissingName placeholders rather than NameError,
    so a single unevaluable annotation cannot discard a whole class's plan and
    every caller receives something to classify. Any other failure -- a syntax
    error, or an operator that rejects a placeholder such as `int | Missing` --
    yields one placeholder standing for the entire expression, which then
    classifies as "not Autowired" and is skipped like any unrelated annotation.

    Returns Any because it passes through whatever eval() produced, exactly as
    the annotation machinery it feeds does.
    """
    if not isinstance(annotation, str):
        return annotation

    try:
        return eval(annotation, module_globals, _MissingNames(module_globals))
    except Exception:
        return _MissingName(annotation)


def callable_hints(func: object) -> dict[str, Any]:
    """Resolve a callable's annotations, tolerating unresolvable ones.

    get_type_hints() is tried first because it handles cases the plain
    evaluator does not, such as implicit Optional. No explicit globalns is
    passed: func may be an __init__ inherited from a base class in a different
    module, so the owning class's module would be the wrong resolution context.
    get_type_hints() reads func.__globals__ internally, which is always the
    module func was defined in.

    get_type_hints() evaluates every annotation at once, so one unresolvable
    annotation would discard them all. The fallback evaluates them one at a
    time through evaluate_annotation, which is total -- so unlike the old
    fallback it never silently drops a parameter.
    """
    try:
        return get_type_hints(func, include_extras=True)
    except NameError:
        pass

    func_globals = getattr(func, "__globals__", {})

    return {
        name: evaluate_annotation(annotation, func_globals)
        for name, annotation in getattr(func, "__annotations__", {}).items()
    }


def resolve_autowired_type(
    annotation: object,
    module_globals: dict[str, Any],
    context: str | None = None,
) -> Any | None:  # noqa: ANN401
    """Return the wrapped type if annotation is Autowired[T], else None.

    Three outcomes, and only one of them is an error:

    1. Autowired[T] with T resolvable -> T.
    2. Autowired[T] with T unresolvable -> AnnotationResolutionError. Returning
       None here would be indistinguishable from "not Autowired", which would
       silently skip the injection the annotation asked for.
    3. Anything else -> None. That includes a non-Autowired annotation which
       itself contains an unresolvable name (list[Missing], a
       TYPE_CHECKING-only import): not pywire's business.

    Args:
        annotation: The annotation to inspect, already evaluated.
        module_globals: Globals a forward reference is evaluated against.
        context: Optional "Owner.member" label, interpolated into the error
            message so a broken annotation names the code that carries it.

    Returns:
        T for case 1, None for case 3.

    Raises:
        AnnotationResolutionError: case 2.

    The return type stays Any: it passes through whatever get_args() extracted,
    unexamined, and typeshed's own get_args() is Any-returning.
    """
    if get_origin(annotation) is not Autowired:
        return None

    (wrapped,) = get_args(annotation)

    # A forward reference *inside* Autowired["X"] is a string literal, not a
    # name, so annotation evaluation never touched it -- get_type_hints and
    # eval_str only evaluate the outer annotation string. Under the PEP 695
    # alias such a reference surfaces as a plain str, not a ForwardRef.
    if isinstance(wrapped, str):
        wrapped = evaluate_annotation(wrapped, module_globals)

    if isinstance(wrapped, _MissingName):
        where = f" on {context!r}" if context else ""
        module_name = module_globals.get("__name__", "<unknown>")

        raise AnnotationResolutionError(
            f"Cannot resolve Autowired[{wrapped.name}]{where}: name "
            f"{wrapped.name!r} is not defined in module {module_name!r}."
        )

    return wrapped
