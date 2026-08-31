from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from .exceptions import RegistrationError

_PRE_DESTROY_MARKER = "__pywire_pre_destroy__"

_POSITIONAL_KINDS = (
    inspect.Parameter.POSITIONAL_ONLY,
    inspect.Parameter.POSITIONAL_OR_KEYWORD,
    inspect.Parameter.VAR_POSITIONAL,
)
_VARIADIC_KINDS = (
    inspect.Parameter.VAR_POSITIONAL,
    inspect.Parameter.VAR_KEYWORD,
)


def pre_destroy[F: Callable[..., object]](func: F) -> F:
    """Mark an instance method as this class's teardown hook.

    A pure tag, no wrapping: the function is returned unchanged, and calling
    it directly behaves exactly as if it were undecorated. Container discovers
    it lazily, at registration time, through find_pre_destroy() -- never at
    decoration time, and never by calling the method itself.
    """
    setattr(func, _PRE_DESTROY_MARKER, True)

    return func


def find_pre_destroy(cls: type) -> tuple[str, Callable[..., object]] | None:
    """Return the (name, function) of cls's @pre_destroy method, if any.

    Walks cls.__mro__ most-derived first. For each attribute name, only the
    *first* class in that walk to define it is ever inspected -- a `seen`
    set blocks every later (more base) class from being considered for that
    name at all. That single pass already implements real MRO override
    semantics: a subclass that overrides the method without re-decorating it
    is the first (and only) definition of that name to be inspected, is not
    marked, and the base's marked version is never reached -- opting the
    subclass out. Same rule already documented for Autowired fields on a
    base class in plans.py.

    Raises:
        RegistrationError: more than one distinct method name survives this
            walk (a bean has at most one teardown hook), the surviving
            method is a coroutine function, or its signature requires more
            than the instance argument.
    """
    survivors: list[tuple[str, Callable[..., object]]] = []
    seen: set[str] = set()

    for owner in cls.__mro__:
        for name, value in vars(owner).items():
            if name in seen:
                continue

            seen.add(name)

            if callable(value) and getattr(value, _PRE_DESTROY_MARKER, False):
                survivors.append((name, value))

    if not survivors:
        return None

    if len(survivors) > 1:
        names = ", ".join(name for name, _ in survivors)

        raise RegistrationError(
            f"'{cls.__name__}' has more than one @pre_destroy method: "
            f"{names}. A bean can have at most one teardown hook."
        )

    name, func = survivors[0]
    label = f"{cls.__name__}.{name}"

    if inspect.iscoroutinefunction(func):
        raise RegistrationError(
            f"'{label}' is a coroutine function: it cannot be used as a "
            "teardown hook."
        )

    _reject_bad_teardown_signature(label, func)

    return name, func


def resolve_teardown(
    cls: type, on_close: Callable[[Any], None] | None
) -> Callable[[Any], None] | None:
    """Reconcile @pre_destroy discovery with an explicit on_close kwarg.

    The single place register()/register_factory()/register_instance() call to
    turn a bean's teardown declaration -- whichever of the two sources it
    came from, or neither -- into one Callable[[Any], None] the container
    only ever has to call one way. Called with the *declared* type: cls for
    register/register_factory, type(instance) for register_instance.

    Raises:
        RegistrationError: both a @pre_destroy method and on_close are
            present (no silent precedence rule to remember), on_close is a
            coroutine function, or on_close's signature requires more than
            the instance argument.
    """
    pre_destroy_method = find_pre_destroy(cls)

    if pre_destroy_method is not None and on_close is not None:
        name, _ = pre_destroy_method

        raise RegistrationError(
            f"'{cls.__name__}' has both an on_close callable and a "
            f"@pre_destroy method ('{name}'). Use only one."
        )

    if on_close is not None:
        label = f"on_close for '{cls.__name__}'"

        if inspect.iscoroutinefunction(on_close):
            raise RegistrationError(
                f"{label} is a coroutine function: it cannot be used as a "
                "teardown hook."
            )

        _reject_bad_teardown_signature(label, on_close)

        return on_close

    if pre_destroy_method is None:
        return None

    name, _ = pre_destroy_method

    def call_pre_destroy(instance: object) -> None:
        """Invoke the instance's @pre_destroy method."""
        getattr(instance, name)()

    return call_pre_destroy


def _reject_bad_teardown_signature(label: str, func: Callable[..., object]) -> None:
    """Refuse a teardown callable that cannot be invoked as func(instance).

    Checked from the signature rather than by calling it: a TypeError raised
    *inside* a legitimate teardown call would otherwise be misreported as "bad
    signature". A signature that cannot be introspected at all -- possible for
    on_close, which may be a lambda, a bound method, or a C-implemented
    callable, unlike a @pre_destroy method which is always a plain function --
    is assumed fine and the call is left to speak for itself, mirroring
    plans.py's _reject_unconstructible_new.
    """
    try:
        parameters = list(inspect.signature(func).parameters.values())
    except (TypeError, ValueError):
        return

    if not parameters or parameters[0].kind not in _POSITIONAL_KINDS:
        raise RegistrationError(
            f"Cannot use '{label}' as a teardown hook: it must accept the "
            "torn-down instance as its first positional argument."
        )

    # parameters[0] is where the instance is passed -- `self` for a
    # @pre_destroy method, the sole argument for an on_close callable.
    for parameter in parameters[1:]:
        if parameter.kind in _VARIADIC_KINDS:
            continue

        if parameter.default is inspect.Parameter.empty:
            raise RegistrationError(
                f"Cannot use '{label}' as a teardown hook: parameter "
                f"'{parameter.name}' has no default. A teardown callable is "
                "invoked with the instance as its only argument."
            )
