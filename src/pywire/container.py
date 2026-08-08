from __future__ import annotations

import inspect
import sys
from typing import Any, TypeVar

from .definitions import BeanDefinition
from .exceptions import DependencyResolutionError
from .markers import _AutowiredMarker

T = TypeVar("T")
type Registry = dict[type, BeanDefinition]

class Container:
    """Dependency Injection container.

    Ogni Container possiede il proprio registry e quindi il proprio scope
    singleton. Due Container distinti possono contenere istanze diverse
    dello stesso componente.
    """

    def __init__(self) -> None:
        self._registry: Registry = dict()

    def register(self, cls: type[T]) -> type[T]:
        """Registra una classe come componente.

        La classe viene registrata senza essere istanziata. La creazione
        dell'istanza avviene lazy al primo resolve().
        """
        if cls in self._registry:
            raise ValueError(
                f"Component '{cls.__name__}' è già registrato."
            )

        self._registry[cls] = BeanDefinition(cls=cls)
        self._instrument(cls)

        return cls

    def resolve(self, target_type: type[T]) -> T:
        """Restituisce il singleton associato a target_type."""
        definition = self._registry.get(target_type)

        if definition is None:
            name = getattr(target_type, "__name__", target_type)
            raise DependencyResolutionError(
                f"Impossibile risolvere '{name}': non è registrato nel container."
            )

        if definition.instance is None:
            definition.instance = target_type()

        return definition.instance

    def get(self, target_type: type[T]) -> T:
        """Alias leggibile per resolve()."""
        return self.resolve(target_type)

    def _instrument(self, cls: type) -> None:
        """Installa __new__ e __init__ necessari al field injection."""

        raw_annotations = dict(inspect.get_annotations(cls))
        original_init = cls.__init__
        original_new: Any = cls.__new__
        module_globals = vars(sys.modules[cls.__module__])

        def resolve_field_type(annotation: Any) -> Any:
            if isinstance(annotation, _AutowiredMarker):
                wrapped = annotation.wrapped_type
                # If wrapped_type is a string (forward reference), try to resolve it
                if isinstance(wrapped, str):
                    try:
                        return eval(wrapped, module_globals)
                    except NameError:
                        return None
                return wrapped

            if isinstance(annotation, str):
                try:
                    evaluated = eval(annotation, module_globals)
                except NameError:
                    return None

                if isinstance(evaluated, _AutowiredMarker):
                    wrapped = evaluated.wrapped_type
                    if isinstance(wrapped, str):
                        try:
                            return eval(wrapped, module_globals)
                        except NameError:
                            return None
                    return wrapped

            return None

        def new(
            target_cls: type,
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            definition = self._registry[target_cls]

            if definition.instance is not None:
                return definition.instance

            if original_new is object.__new__:
                instance = original_new(target_cls)
            else:
                instance = original_new(target_cls, *args, **kwargs)

            # Registrazione anticipata: permette di chiudere dipendenze
            # circolari durante la fase di injection.
            definition.instance = instance

            return instance

        def init(
            instance: Any,
            *args: Any,
            **kwargs: Any,
        ) -> None:
            if getattr(instance, "_di_initialized", False):
                return

            if getattr(instance, "_di_initializing", False):
                # L'istanza è già in costruzione. Un'altra dipendenza
                # circolare ha ottenuto la stessa istanza dal registry.
                return

            instance._di_initializing = True

            for field_name, annotation in raw_annotations.items():
                field_type = resolve_field_type(annotation)

                if field_type is not None:
                    setattr(
                        instance,
                        field_name,
                        self.resolve(field_type),
                    )

            original_init(instance, *args, **kwargs)

            instance._di_initialized = True
            instance._di_initializing = False

        cls.__new__ = new
        cls.__init__ = init
