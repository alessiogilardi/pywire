from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .container import Container

_global_container: Container | None = None


def component[T](cls: type[T], container: Container | None = None) -> type[T]:
    """Decoratore per registrare una classe come componente.

    Se nessun container esplicito è disponibile, usa il global container.
    """
    if not container:
        container = get_global_container()

    container.register(cls)
    return cls


def get_global_container() -> Container:
    """Restituisce il container globale."""
    from .container import Container

    global _global_container

    if _global_container is None:
        _global_container = Container()

    return _global_container


service = component
repository = component
agent = component
client = component
