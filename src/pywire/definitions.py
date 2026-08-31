from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any

from .plans import InjectionPlan


class _Origin(Enum):
    """How a bean's instance is obtained.

    Diagnostic only: no branch in the container reads this. It exists so a
    definition inspected in a debugger states what it is, and so the provider
    model is documented by the code and not only by prose.
    """

    CLASS = auto()
    INSTANCE = auto()
    FACTORY = auto()


@dataclass(slots=True)
class BeanDefinition:
    """Metadata and runtime state of a registered component.

    Attributes:
        cls: The concrete class this definition builds. Not necessarily the
            key it is registered under -- as_type binds a registration to a
            supertype or Protocol -- so construction reads this, never the
            registry key.
        factory: Callable that produces the instance, or None to construct `cls`
            the ordinary way. A pushed instance is a factory returning the object
            it was handed, which is what keeps clear_instances() and rollback
            free of special cases: every bean is rebuildable, and rebuilding a
            pushed one yields the same object.
        origin: How the instance is obtained. Read by humans, never by the
            container.
        instance: The singleton, once created. On the class path it is
            published *before* __init__ runs, so a dependency cycle closing
            through a field can find it. On the factory path publication is
            necessarily late: the object does not exist until the factory
            returns. That asymmetry is why a cycle reaching a factory bean
            can never find a partial instance, and is always rejected.
        ready: True once the instance is complete -- __init__ returned, or the
            factory returned. Because `instance` is published early on the
            class path, it alone is not enough to hand the object out without
            synchronisation -- a reader could observe a half-built object.
            `ready` is what an unsynchronised read can trust. Teardown
            (_roll_back and clear_instances) clears it together with
            `instance`, and must clear `ready` *before* `instance` -- the
            opposite of the order they were set in -- mirroring the order
            resolve()'s lock-free fast path reads them in, since the write
            order is what narrows the window in which an interleaved reader
            could observe a stale `ready=True`.
        plan: Cached InjectionPlan, computed on first resolution rather than at
            registration time: a forward reference to a class defined later in
            the module can only be resolved late. Never cleared, by rollback or
            by clear_instances(), a plan being a pure function of the class.
        teardown: Callable invoked with the finished instance during
            Container.close() or a rollback that discards this bean after it
            reached ready=True. None means no teardown was declared. Set by
            lifecycle.resolve_teardown() at registration time, normalizing
            whichever of @pre_destroy / on_close was used -- Container never
            branches on which one it was.
    """

    cls: type
    factory: Callable[[], object] | None = None
    origin: _Origin = _Origin.CLASS
    instance: object | None = None
    ready: bool = False
    plan: InjectionPlan | None = None
    teardown: Callable[[Any], None] | None = None
