from __future__ import annotations

import inspect
import sys
from typing import Any, get_args, get_origin

from .definitions import BeanDefinition
from .exceptions import DependencyResolutionError
from .markers import Autowired

type Registry = dict[type, BeanDefinition]

class Container:
    """Dependency Injection container.

    Each Container owns its own registry and therefore its own singleton
    scope. Two distinct Containers can hold different instances of the
    same component.
    """

    def __init__(self) -> None:
        self._registry: Registry = dict()

    def register[T](self, cls: type[T]) -> type[T]:
        """Register a class as a component.

        The class is registered without being instantiated. The instance
        is created lazily on the first resolve().
        """
        if cls in self._registry:
            raise ValueError(
                f"Component '{cls.__name__}' is already registered."
            )

        self._registry[cls] = BeanDefinition(cls=cls)
        self._instrument(cls)

        return cls

    def resolve[T](self, target_type: type[T]) -> T:
        """Return the singleton associated with target_type."""
        definition = self._registry.get(target_type)

        if definition is None:
            name = getattr(target_type, "__name__", target_type)
            raise DependencyResolutionError(
                f"Cannot resolve '{name}': it is not registered in the container."
            )

        if definition.instance is None:
            definition.instance = target_type()

        return definition.instance

    def get[T](self, target_type: type[T]) -> T:
        """Readable alias for resolve()."""
        return self.resolve(target_type)

    def _instrument(self, cls: type) -> None:
        """Install the __new__ and __init__ needed for field injection."""

        raw_annotations = inspect.get_annotations(cls, eval_str=True)
        original_init = cls.__init__
        original_new: Any = cls.__new__
        module_globals = vars(sys.modules[cls.__module__])

        def resolve_field_type(annotation: Any) -> Any:
            if get_origin(annotation) is not Autowired:
                return None

            (wrapped,) = get_args(annotation)

            # Forward references inside Autowired["X"] are left unresolved by
            # eval_str=True, since it only evaluates the outer annotation string.
            # Under the PEP 695 alias, such a reference surfaces as a plain str
            # rather than a ForwardRef.
            if isinstance(wrapped, str):
                try:
                    return eval(wrapped, module_globals)
                except NameError:
                    return None

            return wrapped

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

            # Early registration: allows closing circular dependencies
            # during the injection phase.
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
                # The instance is already under construction. Another
                # circular dependency obtained the same instance from
                # the registry.
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
