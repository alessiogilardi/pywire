from __future__ import annotations

from .container import Container

_default_container: Container | None = None


def component[T](cls: type[T]) -> type[T]:
    """Decorator to register a class on the default container."""
    get_default_container().register(cls)
    return cls


def get_default_container() -> Container:
    """Return the default container."""

    global _default_container

    if _default_container is None:
        _default_container = Container()

    return _default_container


service = component
repository = component
agent = component
client = component
