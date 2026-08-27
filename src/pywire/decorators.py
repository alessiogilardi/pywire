from __future__ import annotations

from collections.abc import Callable
from typing import overload

from .container import get_default_container


@overload
def component[T](cls: type[T]) -> type[T]: ...


@overload
def component[T](*, as_type: type) -> Callable[[type[T]], type[T]]: ...


def component[T](
    cls: type[T] | None = None, *, as_type: type | None = None
) -> type[T] | Callable[[type[T]], type[T]]:
    """Decorator to register a class on the default container.

    Usable bare or called with a binding:

        @service
        class UserService: ...

        @repository(as_type=UserRepository)
        class PostgresUserRepo: ...

    The called form **rebinds** the registration key, exactly as
    Container.register(cls, as_type=...) does: the decorated class is no longer
    a key of its own.

    Unlike Container.register, this path does not check the subtype relation
    statically. Python cannot express "a TypeVar bounded by another TypeVar", so
    the choice is between checking the relation and preserving the decorated
    class's own type for callers -- and the latter is worth more.

    Raises:
        TypeError: called with parentheses but without as_type.
    """
    if cls is not None:
        if as_type is not None:
            # The called form -- component(cls, as_type=...) -- is the only
            # place as_type is accepted, and it is not reachable through
            # either @overload. Silently registering cls under itself would
            # discard as_type and register it under the wrong key -- the one
            # silent failure this design does not tolerate anywhere else.
            raise TypeError(
                "component() cannot take both a class and 'as_type'. "
                "Write @component(as_type=...) as a decorator."
            )

        get_default_container().register(cls)

        return cls

    if as_type is None:
        # A single def cannot make a keyword required only in the called form,
        # so the overloads make this a static error and this makes it a runtime
        # one. Nothing is gained by treating @component() as @component: it is
        # a typo, not a shorthand.
        raise TypeError(
            "component() requires 'as_type' when called with parentheses. "
            "Use @component without parentheses to register under the class "
            "itself."
        )

    binding = as_type

    def decorate(target: type[T]) -> type[T]:
        get_default_container().register(target, as_type=binding)

        return target

    return decorate
