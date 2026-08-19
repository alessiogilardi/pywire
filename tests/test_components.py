import pytest

from pywire import Autowired, Container, RegistrationError, component


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
