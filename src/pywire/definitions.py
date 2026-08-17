from dataclasses import dataclass
from enum import StrEnum, auto


class Scope(StrEnum):
    SINGLETON = auto()
    PROTOTYPE = auto()


@dataclass(slots=True)
class BeanDefinition:
    """Metadata and runtime state of a registered component."""

    cls: type
    instance: object | None = None
    scope: Scope = Scope.SINGLETON
