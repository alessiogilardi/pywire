from __future__ import annotations

import inspect
import sys
from dataclasses import dataclass
from typing import Any, get_type_hints

from .markers import resolve_autowired_type

_VARIADIC_KINDS = (
    inspect.Parameter.VAR_POSITIONAL,
    inspect.Parameter.VAR_KEYWORD,
)


@dataclass(slots=True, eq=False)
class InjectionPlan:
    """What a class needs in order to be constructed by a Container.

    Planning is pure inspection: it reads annotations and signatures and never
    instantiates, registers, or resolves anything. A plan carries no failure
    state -- a class the container cannot construct is rejected by raising,
    not by recording a flag for someone else to check.

    Not frozen: frozen would be shallow over the two dicts, and it would
    generate a __hash__ that raises TypeError on them. eq=False keeps identity
    semantics, which is all a per-definition cache needs.

    Attributes:
        fields: Class-level Autowired fields, mapped to the type to inject.
        ctor_params: __init__ parameters annotated Autowired, mapped to the
            type to inject.
    """

    fields: dict[str, type]
    ctor_params: dict[str, type]

    @classmethod
    def for_class(cls, target: type) -> InjectionPlan:
        """Inspect target and describe everything the container must supply."""
        return cls(
            fields=_plan_fields(target),
            ctor_params=_plan_constructor(target),
        )


def _plan_fields(target: type) -> dict[str, type]:
    """Collect the class-level Autowired fields declared on target itself.

    Annotations are evaluated one at a time rather than through
    get_annotations(eval_str=True): a single unevaluable annotation (a
    TYPE_CHECKING-only import, a name defined nowhere) would otherwise raise
    NameError and discard the whole class's plan, including its perfectly
    valid Autowired fields.
    """
    module_globals = _module_globals(target)
    fields: dict[str, type] = {}

    for name, annotation in inspect.get_annotations(target).items():
        evaluated = _evaluate(annotation, module_globals)

        if evaluated is None:
            continue

        field_type = resolve_autowired_type(evaluated, module_globals)

        if field_type is not None:
            fields[name] = field_type

    return fields


def _plan_constructor(target: type) -> dict[str, type]:
    """Collect the Autowired parameters of target's __init__."""
    original_init = target.__init__

    if original_init is object.__init__:
        return {}

    hints = _init_hints(original_init)
    init_globals = getattr(original_init, "__globals__", {})
    parameters = list(inspect.signature(original_init).parameters.values())
    ctor_params: dict[str, type] = {}

    # Skip "self", the first parameter of an instance __init__.
    for parameter in parameters[1:]:
        if parameter.kind in _VARIADIC_KINDS:
            continue

        param_type = resolve_autowired_type(hints.get(parameter.name), init_globals)

        if param_type is not None:
            ctor_params[parameter.name] = param_type

    return ctor_params


def _init_hints(original_init: Any) -> dict[str, Any]:  # noqa: ANN401
    """Resolve __init__'s annotations, tolerating unresolvable ones.

    No explicit globalns is passed: original_init may be inherited from a base
    class defined in a different module, so the subclass's module would be the
    wrong resolution context. get_type_hints() reads original_init.__globals__
    internally, which is always the module __init__ was defined in.

    get_type_hints() evaluates every annotation at once, so one unresolvable
    annotation would discard them all. The fallback resolves them one at a
    time; a parameter that still cannot be resolved is simply absent, and is
    then treated like any other non-injected parameter.

    Any is unavoidable here: original_init is an arbitrary user callable and
    typeshed's get_type_hints() is itself Any-returning.
    """
    try:
        return get_type_hints(original_init, include_extras=True)
    except NameError:
        pass

    hints: dict[str, Any] = {}
    init_globals = getattr(original_init, "__globals__", {})

    for name, annotation in getattr(original_init, "__annotations__", {}).items():
        evaluated = _evaluate(annotation, init_globals)

        if evaluated is not None:
            hints[name] = evaluated

    return hints


def _module_globals(owner: type) -> dict[str, Any]:
    """Return the globals of the module owner was defined in.

    Falls back to an empty mapping for classes whose module is not importable
    (built dynamically, or defined in an exec'd string).
    """
    module = sys.modules.get(owner.__module__)

    return getattr(module, "__dict__", {})


def _evaluate(
    annotation: object,
    module_globals: dict[str, Any],
) -> Any | None:  # noqa: ANN401
    """Evaluate a possibly-stringified annotation, or None if it cannot be.

    Deliberately reproduces the current NameError -> None policy so this task
    is a faithful extraction. Task 3 replaces it with markers.evaluate_
    annotation, which is total and changes that policy on purpose.

    Returns Any because it passes through whatever eval() produced, exactly as
    the annotation machinery it feeds does.
    """
    if not isinstance(annotation, str):
        return annotation

    try:
        return eval(annotation, module_globals)
    except NameError:
        return None
