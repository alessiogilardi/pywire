from __future__ import annotations

import threading

from .container import Container

_default_container: Container | None = None
# Guards the lazy initialisation below. A plain Lock, not an RLock: nothing
# reachable from Container() calls back into get_default_container().
_default_container_lock = threading.Lock()


def component[T](cls: type[T]) -> type[T]:
    """Decorator to register a class on the default container."""
    get_default_container().register(cls)
    return cls


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
