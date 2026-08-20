from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import cast

from .definitions import BeanDefinition
from .exceptions import (
    CircularDependencyError,
    DependencyResolutionError,
    PyWireError,
    RegistrationError,
    UnconstructibleComponentError,
)
from .plans import InjectionPlan, field_label, param_label

type Registry = dict[type, BeanDefinition]


class _EdgeKind(Enum):
    """How a resolution frame was entered.

    Only the distinction between CTOR and the rest carries behavior: a cycle is
    illegal exactly when one of its edges is a constructor argument. ROOT and
    FIELD are behaviourally identical, kept apart only so a printed stack reads
    unambiguously while debugging. A plain Enum, not a StrEnum: nothing consumes
    these as strings.
    """

    ROOT = auto()
    FIELD = auto()
    CTOR = auto()


type _Frame = tuple[type, _EdgeKind]


@dataclass(slots=True)
class _Resolution:
    """State of one in-flight resolve() call.

    This is call-scoped data, not container data: the stack of frames being
    built and the undo log of what has been created so far belong to a single
    top-level resolution and are discarded when it ends. Keeping them here
    rather than on Container is what makes the cycle policy and the rollback
    testable without a container, and it is the same mistake -- construction
    bookkeeping stored on a long-lived object -- that the deleted per-instance
    initialization-tracking flags used to make.
    """

    stack: list[_Frame] = field(default_factory=list)
    created: list[BeanDefinition] = field(default_factory=list)

    def position_of(self, target_type: type) -> int | None:
        """Return target_type's index on the stack, if it is being built."""
        for index, (entry, _) in enumerate(self.stack):
            if entry is target_type:
                return index

        return None

    def chain(self) -> tuple[type, ...]:
        """Return the current resolution chain, outermost first."""
        return tuple(entry for entry, _ in self.stack)

    def chain_through(self, target_type: type, start: int = 0) -> tuple[type, ...]:
        """Return the chain from start, ending at target_type."""
        return (*(entry for entry, _ in self.stack[start:]), target_type)


class Container:
    """Dependency Injection container.

    register() records; resolve() constructs. A registered class is never
    modified, so each Container owns a genuinely independent singleton scope
    and a hand-written Cls() stays plain Python -- unwired.
    """

    def __init__(self) -> None:
        self._registry: Registry = dict()
        # Reentrant: resolution recurses into itself for every dependency. Held
        # across the whole construction path, including user __init__ bodies,
        # because that is what guarantees one singleton per type -- releasing it
        # around __init__ would let two threads both miss the cache and both
        # build. The documented cost is that a component whose __init__ waits on
        # another thread's resolve() deadlocks.
        self._lock = threading.RLock()
        # The resolution in flight, or None. Container state only because the
        # public resolve() signature is fixed, so a reentrant call made from
        # inside a component's __init__ has no other way to find the stack it
        # belongs to. Safe as a single field because the lock admits one thread.
        self._current: _Resolution | None = None

    def register[T](self, cls: type[T]) -> type[T]:
        """Register a class as a component.

        The class is neither instantiated nor modified. Both happen lazily,
        inside resolve().
        """
        with self._lock:
            if cls in self._registry:
                raise RegistrationError(
                    f"Component '{cls.__name__}' is already registered "
                    "in this container."
                )

            self._registry[cls] = BeanDefinition(cls=cls)

        return cls

    def resolve[T](self, target_type: type[T]) -> T:
        """Return the singleton associated with target_type, building it (and
        everything it needs) on first call."""
        definition = self._registry.get(target_type)

        if definition is not None and definition.ready:
            # Unsynchronised fast path, safe only because `ready` is set after
            # __init__ returned: reading `instance` alone would risk handing out
            # the early-published, half-built object. This relies on the GIL
            # ordering the two writes; a free-threaded build would need an
            # explicit acquire/release pair here.
            return cast(T, definition.instance)

        with self._lock:
            resolution = self._current
            outermost = resolution is None

            if resolution is None:
                resolution = _Resolution()
                self._current = resolution

            # A resolve() reached from inside a component's __init__ is, for
            # cycle purposes, a constructor edge: whatever it returns is used
            # immediately, so it has to be complete.
            edge = _EdgeKind.CTOR if resolution.stack else _EdgeKind.ROOT

            try:
                return cast(T, self._resolve(target_type, edge, None, resolution))
            finally:
                if outermost:
                    self._current = None

    def get[T](self, target_type: type[T]) -> T:
        """Readable alias for resolve()."""
        return self.resolve(target_type)

    def clear_instances(self) -> None:
        """Drop every cached singleton, keeping every registration.

        Cached InjectionPlans survive: a plan is a pure function of the class
        and cannot go stale. Intended for test isolation of the module-level
        default container -- which @component writes into and nothing else ever
        resets -- and for any caller that wants a fresh object graph without
        rebuilding the registry.
        """
        with self._lock:
            for definition in self._registry.values():
                # `ready` first, mirroring the order resolve()'s lock-free fast
                # path reads the two fields in: this narrows, but does not
                # close, the window in which a reader interleaved between
                # these two independent, non-atomic writes could observe
                # ready=True with instance already gone.
                definition.ready = False
                definition.instance = None

    def _resolve(
        self,
        target_type: type,
        edge: _EdgeKind,
        requester: str | None,
        resolution: _Resolution,
    ) -> object:
        """Return target_type's singleton, constructing it if necessary.

        `requester` travels *down* the recursion rather than being attached to
        an exception on the way back up, so the frame that raises already knows
        both pieces of context and the exception can stay immutable.
        """
        definition = self._registry.get(target_type)

        if definition is None:
            name = getattr(target_type, "__name__", target_type)

            raise DependencyResolutionError(
                f"Cannot resolve '{name}': it is not registered "
                "in this container.",
                chain=resolution.chain_through(target_type),
                requester=requester,
            )

        position = resolution.position_of(target_type)

        if position is not None:
            self._reject_constructor_cycle(
                target_type, position, edge, requester, resolution
            )

            # A legal field cycle: the partner is still under construction, but
            # its identity is final, which is all a stored reference needs.
            return definition.instance

        if definition.instance is not None:
            return definition.instance

        return self._create(target_type, definition, edge, requester, resolution)

    def _create(
        self,
        target_type: type,
        definition: BeanDefinition,
        edge: _EdgeKind,
        requester: str | None,
        resolution: _Resolution,
    ) -> object:
        """Build, wire, and initialize a single bean."""
        created_mark = len(resolution.created)
        resolution.stack.append((target_type, edge))

        try:
            instance = self._build_from_class(
                target_type, definition, requester, resolution
            )

            # Last, and only on the success path: this is what the
            # unsynchronised fast path in resolve() reads.
            #
            # Guarded, because `instance` may no longer be the object this
            # frame built. clear_instances() takes the same reentrant lock this
            # frame already holds, so a component whose __init__ calls it wipes
            # the very definition under construction. Marking that emptied
            # definition ready would leave ready=True with instance=None -- the
            # one combination the fast path cannot detect -- and every later
            # resolve() would return None cast to T, silently and permanently.
            # The bean is left uncached instead; the caller still receives the
            # object this frame built.
            if definition.instance is instance:
                definition.ready = True

            return instance
        except BaseException:
            self._roll_back(resolution, created_mark)
            raise
        finally:
            resolution.stack.pop()

    def _build_from_class(
        self,
        target_type: type,
        definition: BeanDefinition,
        requester: str | None,
        resolution: _Resolution,
    ) -> object:
        """Allocate target_type, inject its fields, and run its __init__."""
        if definition.plan is None:
            # Planned before allocating: a class that cannot be planned is
            # never created and never published.
            definition.plan = self._plan(target_type, requester, resolution)

        # Cast because pyright resolves target_type.__new__ against the
        # metaclass, whose overloads describe creating a *class*, not
        # allocating an instance. The real signature is only known at
        # runtime anyway.
        allocate = cast(Callable[[type], object], target_type.__new__)
        instance = allocate(target_type)

        # Early publication: a dependency cycle closing through a field
        # finds this instance instead of recursing forever.
        definition.instance = instance
        resolution.created.append(definition)

        # Fields first, deliberately: by the time a component's __init__
        # body runs, its injected fields are set and readable. That is a
        # contract, and the only reason __new__ and __init__ are split.
        self._inject_fields(instance, definition.plan, target_type, resolution)
        kwargs = self._resolve_ctor_args(definition.plan, target_type, resolution)
        target_type.__init__(instance, **kwargs)

        return instance

    def _plan(
        self,
        target_type: type,
        requester: str | None,
        resolution: _Resolution,
    ) -> InjectionPlan:
        """Plan target_type, adding this resolution's context to any failure.

        Planning is pure inspection and knows nothing about the stack, so the
        errors it raises arrive contextless. They are re-raised as copies
        carrying the chain rather than mutated in place -- see
        PyWireError.with_context.
        """
        try:
            return InjectionPlan.for_class(target_type)
        except PyWireError as error:
            raise error.with_context(
                chain=resolution.chain(), requester=requester
            ) from error

    def _roll_back(self, resolution: _Resolution, created_mark: int) -> None:
        """Discard every instance built inside the failing subtree.

        Clearing only the failing bean is not enough: a partner already
        initialized during the same call may hold a reference to a half-built
        object, which would leave the registry handing out an instance the
        container has otherwise disowned.

        Rolling back per *subtree* rather than only at the outermost frame
        matters for a reachable case: a component whose __init__ resolves an
        optional dependency inside a try/except swallows the failure, so it
        never reaches an outer frame at all.

        `ready` is cleared alongside `instance` -- leaving it set would keep a
        rolled-back bean visible to the unsynchronised fast path in resolve()
        forever, which is worse than the problem `ready` solves -- and it is
        cleared *first*, because that fast path reads `ready` before `instance`:
        undoing the writes in the opposite order narrows, but does not close,
        the window in which an interleaved reader could observe a disowned
        instance as ready, since the two stores remain independent and
        non-atomic. `plan` is deliberately preserved: it is a pure function of
        the class, and a construction failure says nothing about its validity.
        """
        for definition in resolution.created[created_mark:]:
            definition.ready = False
            definition.instance = None

        del resolution.created[created_mark:]

    def _inject_fields(
        self,
        instance: object,
        plan: InjectionPlan,
        target_type: type,
        resolution: _Resolution,
    ) -> None:
        """Set every planned Autowired field on the fresh instance."""
        for name, field_type in plan.fields.items():
            label = field_label(target_type, name)
            value = self._resolve(field_type, _EdgeKind.FIELD, label, resolution)

            try:
                setattr(instance, name, value)
            except AttributeError as exc:
                # Plan time rejects frozen dataclasses, which are detectable
                # from the class. This catches whatever else forbids assignment
                # -- __slots__ without a slot for the field being the common one
                # -- because that is not statically decidable: a base class may
                # supply __dict__.
                raise UnconstructibleComponentError(
                    f"Cannot inject field '{name}' into "
                    f"'{target_type.__name__}': the attribute cannot be set. "
                    "Use constructor injection instead.",
                    chain=resolution.chain(),
                ) from exc

    def _resolve_ctor_args(
        self,
        plan: InjectionPlan,
        target_type: type,
        resolution: _Resolution,
    ) -> dict[str, object]:
        """Resolve the Autowired constructor parameters."""
        return {
            name: self._resolve(
                dep_type,
                _EdgeKind.CTOR,
                param_label(target_type, name),
                resolution,
            )
            for name, dep_type in plan.ctor_params.items()
        }

    def _reject_constructor_cycle(
        self,
        target_type: type,
        position: int,
        edge: _EdgeKind,
        requester: str | None,
        resolution: _Resolution,
    ) -> None:
        """Refuse a cycle that passes through a constructor parameter.

        The check is on the *cycle*, not on the current frame. Checking the
        frame ("is this dependency on the stack?") would make a mixed
        field/constructor cycle succeed or fail depending on which type was
        resolved first, since only one of the two entry points reaches the cycle
        through the constructor edge.

        `edge` is included in the scan because the edge closing the cycle has
        not been pushed onto the stack yet. The scan starts at position + 1,
        not at position: the edge recorded on the entry frame is how that type
        was reached from *outside* the cycle, so counting it would reject a
        legal field cycle merely for having been entered through an unrelated
        constructor parameter.
        """
        kinds = [kind for _, kind in resolution.stack[position + 1 :]]
        kinds.append(edge)

        if _EdgeKind.CTOR not in kinds:
            return

        cycle = resolution.chain_through(target_type, start=position)
        rendered = " -> ".join(entry.__name__ for entry in cycle)

        raise CircularDependencyError(
            "Circular dependency through a constructor parameter: "
            f"{rendered}. Convert one of these dependencies to a field "
            "to allow the cycle.",
            chain=cycle,
            requester=requester,
        )
