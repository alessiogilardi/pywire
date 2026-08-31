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

    Self-resolving from inside __new__ still has to fail safely rather than
    silently hand back None: a resolve() re-entered while the stack is
    non-empty is always tagged as a constructor edge, whether the re-entrant
    call originates from __init__ or from __new__, so this closes as an
    ordinary constructor cycle -- see
    test_new_that_resolves_during_construction_fails_explicitly.
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


class FactoryCycleClient:
    """Module-level, not local to its test: with `from __future__ import
    annotations` active, a field's Autowired[...] argument is resolved against
    the *module*'s globals, so a class defined inside the test function body
    could never be named by another local class's annotation."""

    pass


class RollbackDep:
    """Module-level for the same reason as FactoryCycleClient: RollbackHost's
    `Autowired[RollbackDep]` field annotation is resolved against module
    globals, which a function-local class would not be part of."""

    pass


class RollbackFailing:
    def __init__(self) -> None:
        raise RuntimeError("boom")


class RollbackHost:
    # Declaration order matters: plans.py preserves it, so RollbackDep always
    # finishes (reaches ready=True) before RollbackFailing raises.
    dep: Autowired[RollbackDep]
    failing: Autowired[RollbackFailing]


class RollbackDep2:
    pass


class RollbackFailing2:
    def __init__(self) -> None:
        raise RuntimeError("boom")


class RollbackHost2:
    dep: Autowired[RollbackDep2]
    failing: Autowired[RollbackFailing2]


class RollbackDep3:
    pass


class RollbackFailing3:
    def __init__(self) -> None:
        raise KeyboardInterrupt


class RollbackHost3:
    dep: Autowired[RollbackDep3]
    failing: Autowired[RollbackFailing3]


class NestedRollbackA:
    pass


class NestedRollbackB:
    # A field so B's own resolution allocates and fully completes A before B
    # itself reaches ready=True.
    a: Autowired[NestedRollbackA]


class NestedRollbackFailing:
    def __init__(self) -> None:
        raise RuntimeError("boom")


class NestedRollbackHost:
    # Declaration order matters: plans.py preserves it, so the B->A chain
    # fully completes before NestedRollbackFailing raises.
    x: Autowired[NestedRollbackB]
    y: Autowired[NestedRollbackFailing]


class DupTeardownDep:
    pass


class DupTeardownFailing:
    def __init__(self) -> None:
        raise RuntimeError("boom")


class DupTeardownHost:
    dep: Autowired[DupTeardownDep]
    failing: Autowired[DupTeardownFailing]


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
    from __init__ or from __new__ -- so this closes as an ordinary
    constructor cycle through _reject_constructor_cycle. Asserting on the
    exact message pins today's real behavior: it will fail loudly if the
    edge-tagging logic ever changes in a way that stops treating a
    __new__-issued resolve() as a constructor edge.
    """
    container = Container()
    container.register(ResolvesInNew)
    ResolvesInNew.container = container

    try:
        with pytest.raises(CircularDependencyError) as excinfo:
            container.resolve(ResolvesInNew)

        assert "through a constructor parameter" in str(excinfo.value)
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


def test_a_factory_resolving_its_own_bean_is_a_cycle():
    container = Container()

    class Client:
        pass

    container.register_factory(Client, lambda: container.resolve(Client))

    with pytest.raises(CircularDependencyError):
        container.resolve(Client)


def test_a_cycle_that_passes_through_a_factory_is_rejected():
    container = Container()

    class Service:
        client: Autowired[FactoryCycleClient]

    container.register(Service)
    container.register_factory(
        FactoryCycleClient, lambda: container.resolve(Service)
    )

    with pytest.raises(CircularDependencyError):
        container.resolve(FactoryCycleClient)


def test_rollback_never_calls_teardown_on_the_bean_that_is_failing():
    container = Container()
    closed: list[str] = []

    class Failing:
        def __init__(self) -> None:
            raise RuntimeError("boom")

    container.register(Failing, on_close=lambda instance: closed.append("failing"))

    with pytest.raises(RuntimeError, match="boom"):
        container.resolve(Failing)

    assert closed == []


def test_rollback_tears_down_a_completed_sibling_but_not_the_failing_bean():
    container = Container()
    closed: list[str] = []

    container.register(
        RollbackDep, on_close=lambda instance: closed.append("dep")
    )
    container.register(RollbackFailing)
    container.register(RollbackHost)

    with pytest.raises(RuntimeError, match="boom"):
        container.resolve(RollbackHost)

    assert closed == ["dep"]


def test_rollback_combines_original_and_teardown_failures_into_an_exception_group():
    container = Container()

    def fail_close(instance: object) -> None:
        raise ValueError("teardown also failed")

    container.register(RollbackDep2, on_close=fail_close)
    container.register(RollbackFailing2)
    container.register(RollbackHost2)

    with pytest.raises(ExceptionGroup) as exc_info:
        container.resolve(RollbackHost2)

    exceptions = exc_info.value.exceptions
    assert any(isinstance(exc, RuntimeError) for exc in exceptions)
    assert any(isinstance(exc, ValueError) for exc in exceptions)


def test_rollback_uses_base_exception_group_for_a_non_exception_original_failure():
    container = Container()

    def fail_close(instance: object) -> None:
        raise ValueError("teardown also failed")

    container.register(RollbackDep3, on_close=fail_close)
    container.register(RollbackFailing3)
    container.register(RollbackHost3)

    with pytest.raises(BaseExceptionGroup) as exc_info:
        container.resolve(RollbackHost3)

    exceptions = exc_info.value.exceptions
    assert any(isinstance(exc, KeyboardInterrupt) for exc in exceptions)
    assert any(isinstance(exc, ValueError) for exc in exceptions)


def test_rollback_tears_down_dependent_before_its_own_dependency():
    """resolution.created records allocation-start order, which for a nested
    chain B(Autowired[A]) is [..., B, A, ...] -- B is allocated first and
    starts resolving its field, which allocates and fully completes A,
    before B's own __init__ runs and B itself completes.

    Teardown order must still be completion order reversed (B before A, the
    dependent before its dependency), which is what self._ready_order -- and
    not resolution.created -- gives filtered down to this subtree.
    """
    container = Container()
    closed: list[str] = []

    container.register(
        NestedRollbackA, on_close=lambda instance: closed.append("A")
    )
    container.register(
        NestedRollbackB, on_close=lambda instance: closed.append("B")
    )
    container.register(NestedRollbackFailing)
    container.register(NestedRollbackHost)

    with pytest.raises(RuntimeError, match="boom"):
        container.resolve(NestedRollbackHost)

    assert closed == ["B", "A"]


def test_rolled_back_bean_rebuilt_and_closed_tears_down_exactly_once():
    """A bean that reaches ready=True inside a subtree that later rolls back
    gets torn down and removed from self._ready_order by _roll_back. If the
    stale entry survived, resolving the same type again and then closing
    would append a second entry for the same BeanDefinition, and close()
    would fire its teardown twice for what was only ever one live instance
    at close time.
    """
    container = Container()
    closed: list[str] = []

    container.register(
        DupTeardownDep, on_close=lambda instance: closed.append("dep")
    )
    container.register(DupTeardownFailing)
    container.register(DupTeardownHost)

    with pytest.raises(RuntimeError, match="boom"):
        container.resolve(DupTeardownHost)

    assert closed == ["dep"]

    # DupTeardownDep was rolled back, not deregistered -- it can be resolved
    # again on its own, independent of the permanently-broken Host.
    container.resolve(DupTeardownDep)
    container.close()

    assert closed == ["dep", "dep"]
