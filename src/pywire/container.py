from __future__ import annotations

import inspect
import sys
from typing import Any, get_type_hints

from .definitions import BeanDefinition
from .exceptions import DependencyResolutionError
from .markers import resolve_autowired_type

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

        # Deliberately not passing globalns=module_globals here: original_init
        # may be inherited from a base class defined in a different module
        # than cls, in which case cls.__module__ would be the wrong module.
        # get_type_hints() with no explicit globalns already resolves
        # correctly on its own, since it reads original_init.__globals__
        # internally, which is always the module __init__ was defined in.
        #
        # get_type_hints() evaluates *every* annotation on original_init, so
        # a single unresolvable one (e.g. a TYPE_CHECKING-only import, or a
        # forward reference to a name not yet defined) raises NameError and
        # would abort registration for the whole class -- even for classes
        # with no Autowired parameters at all. Fall back to resolving hints
        # one parameter at a time so an unrelated unresolvable annotation
        # cannot poison the rest of the class's registration. A parameter
        # that still can't be resolved is simply left out of init_hints,
        # which is safe: it will be treated as "not Autowired" below, same
        # as any other non-injected parameter.
        try:
            init_hints = get_type_hints(original_init, include_extras=True)
        except NameError:
            init_hints = {}
            init_globals = getattr(original_init, "__globals__", {})
            for name, ann in getattr(original_init, "__annotations__", {}).items():
                try:
                    init_hints[name] = (
                        eval(ann, init_globals) if isinstance(ann, str) else ann
                    )
                except NameError:
                    continue
        # Skip "self" (the first parameter of an instance __init__).
        ctor_param_names = list(inspect.signature(original_init).parameters)[1:]
        ctor_autowired_params = {
            name: target
            for name in ctor_param_names
            if (target := resolve_autowired_type(init_hints.get(name), module_globals))
            is not None
        }

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
                field_type = resolve_autowired_type(annotation, module_globals)

                if field_type is not None:
                    setattr(
                        instance,
                        field_name,
                        self.resolve(field_type),
                    )

            # Explicitly passed keyword arguments win over auto-resolution.
            # Note: in an A<->B cycle resolved via constructor injection, the
            # partner obtained through self.resolve(...) here is the
            # not-yet-initialized instance (its __init__ has not run yet,
            # since resolving its own constructor args is still in
            # progress) -- analogous to, but not identical to, the
            # field-injection case where some fields may already be set via
            # setattr before the circular call.
            resolved_kwargs = {
                name: self.resolve(target)
                for name, target in ctor_autowired_params.items()
                if name not in kwargs
            }

            original_init(instance, *args, **resolved_kwargs, **kwargs)

            instance._di_initialized = True
            instance._di_initializing = False

        cls.__new__ = new
        cls.__init__ = init
