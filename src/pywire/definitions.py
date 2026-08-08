from dataclasses import dataclass
from enum import StrEnum, auto
from typing import Any


class Scope(StrEnum):
    SINGLETON = auto()
    PROTOTYPE = auto()


@dataclass(slots=True)
class BeanDefinition:
    """Metadata e stato runtime di un componente registrato."""

    cls: type
    instance: Any | None = None
    scope: Scope = Scope.SINGLETON
