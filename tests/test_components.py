import threading
import time

import pytest

from pywire import (
    Autowired,
    Container,
    RegistrationError,
    component,
    get_default_container,
    pre_destroy,
    repository,
)
from pywire import container as container_module


def test_simple_component_resolution():
    """Test basic component registration and resolution."""
    container = Container()

    @component
    class SimpleService:
        pass

    container.register(SimpleService)
    instance = container.resolve(SimpleService)

    assert instance is not None
    assert isinstance(instance, SimpleService)


def test_singleton_behavior():
    """Test that resolved instances are singletons within a container."""
    container = Container()

    @component
    class SingletonService:
        pass

    container.register(SingletonService)

    instance1 = container.resolve(SingletonService)
    instance2 = container.resolve(SingletonService)

    assert instance1 is instance2


def test_dependency_injection_with_autowired():
    """Test field injection via Autowired marker."""
    container = Container()

    @component
    class Repository:
        pass

    @component
    class Service:
        repo: Autowired[Repository]

    container.register(Repository)
    container.register(Service)

    service = container.resolve(Service)

    assert hasattr(service, "repo")
    assert isinstance(service.repo, Repository)
    assert service.repo is container.resolve(Repository)


def test_forward_reference():
    """Test forward reference resolution using direct references."""
    container = Container()

    class ForwardRefServiceA:
        pass

    class ForwardRefServiceB:
        service_a: Autowired[ForwardRefServiceA]

    container.register(ForwardRefServiceA)
    container.register(ForwardRefServiceB)

    service_b = container.resolve(ForwardRefServiceB)

    assert hasattr(service_b, "service_a")
    assert isinstance(service_b.service_a, ForwardRefServiceA)


class SharedDep:
    pass


class SharedService:
    dep: Autowired[SharedDep]


def test_multiple_containers_independent():
    """The same class registered in two containers must yield two distinct
    singletons, along with two distinct dependency graphs.

    The previous version of this test registered *different* classes in the
    two containers, so its assertion was true by construction.
    """
    container1 = Container()
    container2 = Container()

    for container in (container1, container2):
        container.register(SharedDep)
        container.register(SharedService)

    service1 = container1.resolve(SharedService)
    service2 = container2.resolve(SharedService)

    assert service1 is not service2
    assert service1.dep is not service2.dep
    assert service1.dep is container1.resolve(SharedDep)
    assert service2.dep is container2.resolve(SharedDep)


def test_unregistered_component_raises_error():
    """Test that resolving unregistered components raises an error."""
    container = Container()

    class UnregisteredService:
        pass

    from pywire import DependencyResolutionError

    with pytest.raises(DependencyResolutionError):
        container.resolve(UnregisteredService)


def test_duplicate_registration_raises_error():
    """Test that registering the same component twice raises an error."""
    container = Container()

    @component
    class DuplicateService:
        pass

    container.register(DuplicateService)

    with pytest.raises(RegistrationError, match="is already registered"):
        container.register(DuplicateService)


def test_get_alias():
    """Test that get() works as an alias for resolve()."""
    container = Container()

    @component
    class Service:
        pass

    container.register(Service)

    instance_via_get = container.get(Service)
    instance_via_resolve = container.resolve(Service)

    assert instance_via_get is instance_via_resolve


def test_default_container_is_created_once_under_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Threads racing the very first @component must all get one container.

    get_default_container() lazily initialises a module-level global. Without
    a guard, every thread arriving before the first assignment builds its own
    container, and all but the last are dropped -- silently taking their
    registrations with them, since @component returns the class either way.

    The window is widened deterministically rather than raced for: a bare
    barrier reproduces this only about one run in six, which is too flaky to
    protect anything.
    """
    monkeypatch.setattr(container_module, "_default_container", None)

    class SlowContainer(Container):
        def __init__(self) -> None:
            time.sleep(0.05)
            super().__init__()

    monkeypatch.setattr(container_module, "Container", SlowContainer)

    worker_count = 8
    barrier = threading.Barrier(worker_count)
    seen: list[Container] = []
    seen_lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        resolved_container = get_default_container()

        with seen_lock:
            seen.append(resolved_container)

    threads = [threading.Thread(target=worker) for _ in range(worker_count)]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join()

    assert len(seen) == worker_count
    assert len({id(container) for container in seen}) == 1


def test_the_parameterized_decorator_binds_to_a_supertype():
    from typing import Protocol

    class Greeter(Protocol):
        def greet(self) -> str: ...

    @repository(as_type=Greeter)
    class ItalianGreeter:
        def greet(self) -> str:
            return "ciao"

    resolved = get_default_container().resolve(Greeter)

    assert isinstance(resolved, ItalianGreeter)


def test_the_parameterized_decorator_returns_the_decorated_class():
    class Marker:
        pass

    @component(as_type=Marker)
    class Concrete(Marker):
        pass

    assert Concrete.__name__ == "Concrete"
    assert issubclass(Concrete, Marker)


def test_calling_the_decorator_with_no_arguments_is_an_error():
    with pytest.raises(TypeError, match="as_type"):

        @component()  # type: ignore[call-overload]
        class Service:
            pass


def test_passing_a_class_and_as_type_together_is_an_error():
    class Marker:
        pass

    with pytest.raises(TypeError, match="cannot take both"):
        component(Marker, as_type=Marker)  # type: ignore[call-overload]


def test_component_class_teardown_runs_via_the_default_container():
    """The requirement this whole feature exists for: teardown must work with
    nothing but @component -- no explicit Container.register call anywhere."""
    calls: list[str] = []

    @component
    class Resource:
        @pre_destroy
        def shutdown(self) -> None:
            calls.append("closed")

    get_default_container().resolve(Resource)
    get_default_container().close()

    assert calls == ["closed"]
