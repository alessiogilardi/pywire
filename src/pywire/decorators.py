from __future__ import annotations

import threading
from collections.abc import Callable
from typing import overload

from .container import Container

_default_container: Container | None = None
# Guards the lazy initialisation below. A plain Lock, not an RLock: nothing
# reachable from Container() calls back into get_default_container().
_default_container_lock = threading.Lock()


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


def get_default_container() -> Container:
    """Return the default container, creating it on first use.

    Double-checked: the outer test is unsynchronised so the steady state costs
    nothing, and the inner one runs under the lock because several threads can
    pass the outer test before any of them assigns. Without the inner check the
    losers would each build their own container and overwrite the winner's,
    silently discarding everything registered into it -- @component returns the
    class either way, so the loss is invisible until a resolve() fails.
    """
    global _default_container

    if _default_container is None:
        with _default_container_lock:
            if _default_container is None:
                _default_container = Container()

    return _default_container


service = component
repository = component
agent = component
client = component
