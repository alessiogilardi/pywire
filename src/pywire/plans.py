from __future__ import annotations

import inspect
import sys
from dataclasses import dataclass
from typing import Any

from .markers import callable_hints, evaluate_annotation, resolve_autowired_type

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


def field_label(owner: type, name: str) -> str:
    """Return the "Owner.field" label used in error messages."""
    return f"{owner.__qualname__}.{name}"


def param_label(owner: type, name: str) -> str:
    """Return the "Owner.__init__(param)" label used in error messages."""
    return f"{owner.__qualname__}.__init__({name})"


def _plan_fields(target: type) -> dict[str, type]:
    """Collect the class-level Autowired fields declared on target itself."""
    module_globals = _module_globals(target)
    fields: dict[str, type] = {}

    for name, annotation in inspect.get_annotations(target).items():
        evaluated = evaluate_annotation(annotation, module_globals)
        field_type = resolve_autowired_type(
            evaluated, module_globals, field_label(target, name)
        )

        if field_type is not None:
            fields[name] = field_type

    return fields


def _plan_constructor(target: type) -> dict[str, type]:
    """Collect the Autowired parameters of target's __init__."""
    original_init = target.__init__

    if original_init is object.__init__:
        return {}

    hints = callable_hints(original_init)
    init_globals = getattr(original_init, "__globals__", {})
    parameters = list(inspect.signature(original_init).parameters.values())
    ctor_params: dict[str, type] = {}

    # Skip "self", the first parameter of an instance __init__.
    for parameter in parameters[1:]:
        if parameter.kind in _VARIADIC_KINDS:
            continue

        param_type = resolve_autowired_type(
            hints.get(parameter.name),
            init_globals,
            param_label(target, parameter.name),
        )

        if param_type is not None:
            ctor_params[parameter.name] = param_type

    return ctor_params


def _module_globals(owner: type) -> dict[str, Any]:
    """Return the globals of the module owner was defined in.

    Falls back to an empty mapping for classes whose module is not importable
    (built dynamically, or defined in an exec'd string).
    """
    module = sys.modules.get(owner.__module__)

    return getattr(module, "__dict__", {})
