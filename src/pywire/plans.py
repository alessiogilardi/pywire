from __future__ import annotations

import inspect
import sys
from dataclasses import dataclass
from typing import Any

from .exceptions import UnconstructibleComponentError
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
        """Inspect target and describe everything the container must supply.

        Raises:
            UnconstructibleComponentError: target cannot be constructed by the
                container at all -- its __new__ needs arguments, its __init__
                needs an argument nothing can supply, an Autowired parameter is
                positional-only, or it forbids setting an injected field.
            AnnotationResolutionError: an Autowired annotation on target names
                a type that cannot be resolved.
        """
        _reject_unconstructible_new(target)

        ctor_params = _plan_constructor(target)
        # A dataclass declares its Autowired fields as *both* class-level
        # annotations and __init__ parameters. The constructor wins: injecting
        # in both places would resolve twice and would make a frozen dataclass
        # look unconstructible when it is not.
        fields = {
            name: field_type
            for name, field_type in _plan_fields(target).items()
            if name not in ctor_params
        }

        _reject_unsettable_fields(target, fields)

        return cls(fields=fields, ctor_params=ctor_params)


def field_label(owner: type, name: str) -> str:
    """Return the "Owner.field" label used in error messages."""
    return f"{owner.__qualname__}.{name}"


def param_label(owner: type, name: str) -> str:
    """Return the "Owner.__init__(param)" label used in error messages."""
    return f"{owner.__qualname__}.__init__({name})"


def _plan_fields(target: type) -> dict[str, type]:
    """Collect the Autowired fields of target and of every class it inherits.

    The MRO is walked base-first, so a subclass annotation overrides its base's:
    re-annotating a field without Autowired is the supported way to opt out of
    an inherited injection.

    Each annotation is resolved against *its own* defining class's module
    globals. Using target's module would be wrong for an inherited annotation,
    the same trap the constructor path already avoids.

    Annotations are evaluated through markers.evaluate_annotation, which is
    total: a single unevaluable annotation (a TYPE_CHECKING-only import, a name
    defined nowhere) cannot discard the whole class's plan, and an annotation
    that fails to resolve *and* is recognisably Autowired is reported by
    resolve_autowired_type rather than skipped.
    """
    fields: dict[str, type] = {}

    for owner in reversed(target.__mro__):
        if owner is object:
            continue

        owner_globals = _module_globals(owner)

        for name, annotation in inspect.get_annotations(owner).items():
            evaluated = evaluate_annotation(annotation, owner_globals)
            field_type = resolve_autowired_type(
                evaluated, owner_globals, field_label(owner, name)
            )

            if field_type is None:
                fields.pop(name, None)
            else:
                fields[name] = field_type

    return fields


def _reject_unconstructible_new(target: type) -> None:
    """Refuse a class whose __new__ needs arguments resolve() cannot supply.

    Checked from the signature rather than by catching TypeError around
    target.__new__(target): a TypeError raised *inside* a legitimate __new__
    would otherwise be reported as "its __new__ requires arguments", which is
    actively misleading. A signature that cannot be introspected at all is
    assumed fine, and construction is left to speak for itself.
    """
    try:
        signature = inspect.signature(target.__new__)
    except (TypeError, ValueError):
        return

    named = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind not in _VARIADIC_KINDS
    ]

    # named[0], when present, is "cls". A class that does not override __new__
    # inherits object.__new__, whose signature is (*args, **kwargs) -- so
    # `named` is empty and the loop below does nothing, which is correct.
    for parameter in named[1:]:
        if parameter.default is inspect.Parameter.empty:
            raise UnconstructibleComponentError(
                f"Cannot construct '{target.__name__}': its __new__ requires "
                f"argument '{parameter.name}'. Register a pre-built instance "
                "instead."
            )


def _reject_unsettable_fields(target: type, fields: dict[str, type]) -> None:
    """Refuse a frozen class that still needs a field set on it.

    Only reachable after the constructor/field dedup: a frozen dataclass whose
    Autowired fields are all constructor parameters has nothing left to set and
    is perfectly constructible.

    Frozen is checked here, statically, because it is a property of the class.
    The other way an attribute can refuse assignment -- __slots__ with no slot
    for the field -- is *not* statically decidable, since a base class may
    supply __dict__, so it stays a runtime rejection in Container._inject_fields.
    """
    if not fields:
        return

    params = getattr(target, "__dataclass_params__", None)

    if not getattr(params, "frozen", False):
        return

    name = next(iter(fields))

    raise UnconstructibleComponentError(
        f"Cannot inject field '{name}' into frozen '{target.__name__}': "
        "use constructor injection instead."
    )


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
            if parameter.kind is inspect.Parameter.POSITIONAL_ONLY:
                # Resolved dependencies are passed by keyword, so a
                # positional-only parameter would die on **kwargs with a bare
                # TypeError far from its cause.
                raise UnconstructibleComponentError(
                    f"Cannot construct '{target.__name__}': Autowired "
                    f"parameter '{parameter.name}' is positional-only. "
                    "Remove the '/' marker, or inject it as a field."
                )

            ctor_params[parameter.name] = param_type
        elif parameter.default is inspect.Parameter.empty:
            # resolve() passes no arguments, so a parameter that is neither
            # Autowired nor defaulted can never be satisfied. Today this
            # surfaces as a bare TypeError from Python, far from the cause.
            raise UnconstructibleComponentError(
                f"Cannot construct '{target.__name__}': parameter "
                f"'{parameter.name}' has no default and is not Autowired. "
                "Register an instance instead, or give it a default."
            )

    return ctor_params


def _module_globals(owner: type) -> dict[str, Any]:
    """Return the globals of the module owner was defined in.

    Falls back to an empty mapping for classes whose module is not importable
    (built dynamically, or defined in an exec'd string).
    """
    module = sys.modules.get(owner.__module__)

    return getattr(module, "__dict__", {})
