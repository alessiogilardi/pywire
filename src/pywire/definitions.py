from __future__ import annotations

from dataclasses import dataclass

from .plans import InjectionPlan


@dataclass(slots=True)
class BeanDefinition:
    """Metadata and runtime state of a registered component.

    Attributes:
        cls: The registered class.
        instance: The singleton, once created. Published *before* __init__ runs,
            so a dependency cycle closing through a field can find it.
        ready: True once __init__ has returned. Because `instance` is published
            early, it alone is not enough to hand the object out without
            synchronisation -- a reader could observe a half-built object.
            `ready` is what an unsynchronised read can trust. Rollback clears
            it together with `instance`.
        plan: Cached InjectionPlan, computed on first resolution rather than at
            registration time: a forward reference to a class defined later in
            the module can only be resolved late. Never cleared, by rollback or
            by clear_instances(), a plan being a pure function of the class.
    """

    cls: type
    instance: object | None = None
    ready: bool = False
    plan: InjectionPlan | None = None
