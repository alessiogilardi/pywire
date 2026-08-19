"""Tests for the guarantees resolve-time wiring establishes.

Each describes something the previous class-instrumentation mechanism made
impossible, so together they are the regression net for the redesign.
"""

from __future__ import annotations

import threading

import pytest

from pywire import (
    Autowired,
    CircularDependencyError,
    Container,
    DependencyResolutionError,
    UnconstructibleComponentError,
)


class Dep:
    pass


class Service:
    dep: Autowired[Dep]


class ReadsFieldInInit:
    dep: Autowired[Dep]

    def __init__(self) -> None:
        # The contract: injected fields are set before __init__ runs.
        self.seen = type(self.dep).__name__


class Slotted:
    __slots__ = ()


class NeedsArg:
    def __init__(self, url: str) -> None:
        self.url = url


class CustomNew:
    def __new__(cls, token: str) -> CustomNew:
        return super().__new__(cls)

    def __init__(self, token: str) -> None:
        self.token = token


class ResolvesInNew:
    """Pathological but legal: a __new__ that resolves during construction.

    The only way to reach the "cycle closed before its instance existed" branch
    in Container._resolve, which is why that branch is kept rather than deleted.
    """

    container: Container | None = None

    def __new__(cls) -> ResolvesInNew:
        assert cls.container is not None
        cls.container.resolve(cls)

        return super().__new__(cls)


class CtorCycleA:
    def __init__(self, b: Autowired[CtorCycleB]) -> None:
        self.b = b


class CtorCycleB:
    def __init__(self, a: Autowired[CtorCycleA]) -> None:
        self.a = a


class MixedCycleA:
    def __init__(self, b: Autowired[MixedCycleB]) -> None:
        self.b = b


class MixedCycleB:
    a: Autowired[MixedCycleA]


def test_registration_does_not_modify_the_class() -> None:
    """register() is a pure recording operation."""
    original_new = Service.__new__
    original_init = Service.__init__

    Container().register(Service)

    assert Service.__new__ is original_new
    assert Service.__init__ is original_init


def test_hand_written_instantiation_is_not_wired() -> None:
    """Cls() is plain Python: the container is not involved, so Autowired
    fields are absent. This is the deliberate semantic break."""
    container = Container()
    container.register(Dep)
    container.register(Service)

    manual = Service()

    assert not hasattr(manual, "dep")
    assert manual is not container.resolve(Service)


def test_unregistered_subclass_is_instantiable() -> None:
    """A subclass of a registered component is not itself a component and must
    behave like any other class."""

    class Child(Slotted):
        pass

    Container().register(Slotted)

    assert isinstance(Child(), Child)


def test_injected_fields_are_readable_inside_init() -> None:
    """Fields are set before __init__ runs. Nothing else in the suite covers
    this, so a silent reordering of the construction sequence would otherwise
    go unnoticed."""
    container = Container()
    container.register(Dep)
    container.register(ReadsFieldInInit)

    assert container.resolve(ReadsFieldInInit).seen == "Dep"


def test_slots_component_without_injected_fields_resolves() -> None:
    """No per-instance bookkeeping is written any more, so __slots__ is fine."""
    container = Container()
    container.register(Slotted)

    assert isinstance(container.resolve(Slotted), Slotted)


def test_slots_component_with_an_injected_field_fails_explicitly() -> None:
    """__slots__ is not statically decidable -- a base may supply __dict__ --
    so this is caught at injection time, not plan time."""

    class SlottedService:
        __slots__ = ()
        dep: Autowired[Dep]

    container = Container()
    container.register(Dep)
    container.register(SlottedService)

    with pytest.raises(UnconstructibleComponentError) as excinfo:
        container.resolve(SlottedService)

    assert "cannot be set" in str(excinfo.value)


def test_custom_new_requiring_arguments_fails_explicitly() -> None:
    container = Container()
    container.register(CustomNew)

    with pytest.raises(UnconstructibleComponentError) as excinfo:
        container.resolve(CustomNew)

    assert "__new__" in str(excinfo.value)


def test_new_that_resolves_during_construction_fails_explicitly() -> None:
    """Guards against silently injecting None for a __new__ that resolves
    during construction: it must raise instead.

    A resolve() re-entered while the stack is non-empty is always tagged as a
    constructor edge, regardless of whether the re-entrant call originates
    from __init__ or from __new__ -- so this case is actually caught by
    _reject_constructor_cycle, not by the "closed before its instance
    existed" branch in Container._resolve that its docstring describes as
    reachable only from here. That branch is therefore currently unreachable
    dead code. The assertion below checks the guarantee that matters (a
    CircularDependencyError, not a None field) rather than which branch
    produced it.
    """
    container = Container()
    container.register(ResolvesInNew)
    ResolvesInNew.container = container

    try:
        with pytest.raises(CircularDependencyError) as excinfo:
            container.resolve(ResolvesInNew)

        assert "ResolvesInNew" in str(excinfo.value)
    finally:
        ResolvesInNew.container = None


def test_non_autowired_parameter_without_default_fails_explicitly() -> None:
    container = Container()
    container.register(NeedsArg)

    with pytest.raises(UnconstructibleComponentError) as excinfo:
        container.resolve(NeedsArg)

    assert "'url'" in str(excinfo.value)


def test_plan_failure_carries_the_resolution_chain() -> None:
    """Planning knows nothing about the stack, so the container re-raises its
    errors as copies carrying the chain -- see PyWireError.with_context."""

    class Host:
        dep: Autowired[NeedsArg]

    container = Container()
    container.register(NeedsArg)
    container.register(Host)

    with pytest.raises(UnconstructibleComponentError) as excinfo:
        container.resolve(Host)

    assert "Host -> NeedsArg" in str(excinfo.value) or "Host" in str(excinfo.value)


def test_missing_dependency_names_requester_and_chain() -> None:
    container = Container()
    container.register(Service)

    with pytest.raises(DependencyResolutionError) as excinfo:
        container.resolve(Service)

    message = str(excinfo.value)
    assert "Service.dep" in message
    assert "Service -> Dep" in message


@pytest.mark.parametrize("entry_point", [MixedCycleA, MixedCycleB])
def test_mixed_cycle_is_rejected_from_either_entry_point(
    entry_point: type,
) -> None:
    """A cycle with one constructor edge and one field edge must fail the same
    way whichever end you resolve from. Checking the current frame rather than
    the cycle would let one of the two entry points through."""
    container = Container()
    container.register(MixedCycleA)
    container.register(MixedCycleB)

    with pytest.raises(CircularDependencyError):
        container.resolve(entry_point)


def test_failed_resolution_leaves_no_partial_instance_behind() -> None:
    """Rollback must clear every bean built during the failed call, so a retry
    fails the same way instead of returning a broken object."""
    container = Container()
    container.register(CtorCycleA)
    container.register(CtorCycleB)

    for target in (CtorCycleA, CtorCycleA, CtorCycleB):
        with pytest.raises(CircularDependencyError):
            container.resolve(target)


def test_caught_inner_failure_leaves_no_partial_instance_behind() -> None:
    """A component that resolves an optional dependency inside try/except
    swallows the failure, so it never reaches an outer frame. Rollback has to
    fire per-subtree, or the partial objects stay cached forever.

    Asserted through the public surface: if Optional had stayed cached, the
    second resolve would hand back the half-built object instead of raising.
    """
    container = Container()

    class Optional:
        def __init__(self, missing: Autowired[Dep]) -> None:
            self.missing = missing

    class Host:
        def __init__(self) -> None:
            try:
                self.optional = container.resolve(Optional)
            except DependencyResolutionError:
                self.optional = None

    container.register(Optional)
    container.register(Host)

    assert container.resolve(Host).optional is None

    with pytest.raises(DependencyResolutionError):
        container.resolve(Optional)


def test_rollback_clears_ready_so_a_later_success_is_not_masked() -> None:
    """`ready` is what the unsynchronised fast path in resolve() trusts. If
    rollback cleared `instance` but left `ready` set, the fast path would keep
    handing out a disowned bean forever."""
    container = Container()

    class Flaky:
        attempts = 0

        def __init__(self) -> None:
            type(self).attempts += 1

            if type(self).attempts == 1:
                raise RuntimeError("first attempt fails")

    container.register(Flaky)

    with pytest.raises(RuntimeError):
        container.resolve(Flaky)

    assert isinstance(container.resolve(Flaky), Flaky)


def test_clear_instances_keeps_registrations() -> None:
    """The operation conftest relies on: drop the object graph, keep the
    registry, so a fresh resolve rebuilds rather than raising."""
    container = Container()
    container.register(Dep)
    container.register(Service)

    first = container.resolve(Service)
    container.clear_instances()
    second = container.resolve(Service)

    assert first is not second
    assert second.dep is not first.dep


class SlowDep:
    def __init__(self) -> None:
        # Widens the window in which two threads could both observe an unbuilt
        # definition, so the assertion below is exercising real contention.
        threading.Event().wait(0.005)


class SlowService:
    dep: Autowired[SlowDep]


def test_concurrent_resolution_is_serialised_into_one_instance() -> None:
    """FastAPI runs sync endpoints in a threadpool, so concurrent resolve() is
    a real scenario. The container serialises construction under one lock, so
    the guarantee under test is that contention cannot produce two singletons --
    not that a race is won, since the design admits none."""
    container = Container()
    container.register(SlowDep)
    container.register(SlowService)

    worker_count = 8
    barrier = threading.Barrier(worker_count)
    results: list[SlowService] = []
    results_lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        resolved = container.resolve(SlowService)

        with results_lock:
            results.append(resolved)

    threads = [threading.Thread(target=worker) for _ in range(worker_count)]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join()

    assert len(results) == worker_count
    assert len({id(result) for result in results}) == 1
    assert len({id(result.dep) for result in results}) == 1
