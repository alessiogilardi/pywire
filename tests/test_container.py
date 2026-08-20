
from typing import override

import pytest

from pywire import Autowired, Container, component, get_default_container
from pywire.definitions import BeanDefinition


def test_container_initialization():
    """Test that Container initializes correctly."""
    container = Container()
    assert container is not None
    assert isinstance(container, Container)


def test_register_and_resolve():
    """Test basic register and resolve flow."""
    container = Container()

    @component
    class MyService:
        pass

    container.register(MyService)
    instance = container.resolve(MyService)

    assert instance is not None
    assert isinstance(instance, MyService)


def test_multiple_services():
    """Test registering and resolving multiple services."""
    container = Container()

    @component
    class ServiceA:
        pass

    @component
    class ServiceB:
        pass

    container.register(ServiceA)
    container.register(ServiceB)

    service_a = container.resolve(ServiceA)
    service_b = container.resolve(ServiceB)

    assert isinstance(service_a, ServiceA)
    assert isinstance(service_b, ServiceB)
    assert service_a is not service_b


def test_complex_dependency_graph():
    """Test a more complex dependency graph."""
    container = Container()

    @component
    class Database:
        pass

    @component
    class Repository:
        db: Autowired[Database]

    @component
    class Service:
        repo: Autowired[Repository]

    @component
    class Controller:
        service: Autowired[Service]

    container.register(Database)
    container.register(Repository)
    container.register(Service)
    container.register(Controller)

    controller = container.resolve(Controller)

    assert isinstance(controller.service, Service)
    assert isinstance(controller.service.repo, Repository)
    assert isinstance(controller.service.repo.db, Database)


def test_bean_definition_metadata():
    """Test that BeanDefinition properly tracks component metadata."""

    container = Container()

    @component
    class MetaService:
        pass

    container.register(MetaService)
    instance = container.resolve(MetaService)

    # Access internal registry to verify BeanDefinition
    definition = container._registry[MetaService]
    assert definition.cls is MetaService
    assert definition.instance is instance


def test_default_container_functionality():
    """Test the default container functionality."""
    default_container = get_default_container()

    assert default_container is not None
    assert isinstance(default_container, Container)

    # Get it again to verify singleton behavior
    same_container = get_default_container()
    assert same_container is default_container


class FailingComponent:
    """__init__ always raises, so resolving it always rolls back."""

    def __init__(self) -> None:
        raise RuntimeError("construction failed")


class ClearableComponent:
    pass


class _WriteOrderDefinition(BeanDefinition):
    """BeanDefinition recording every write to `ready` and `instance`.

    Teardown order is a real invariant -- resolve()'s lock-free fast path
    reads `ready` and only then `instance`, so clearing `instance` first would
    let an interleaved reader be handed None -- but it is invisible to a
    single-threaded observer. Recording the writes pins the order without
    racing threads.
    """

    def __init__(self, cls: type) -> None:
        self.writes: list[str] = []
        super().__init__(cls=cls)

    @override
    def __setattr__(self, name: str, value: object) -> None:
        if name in ("ready", "instance"):
            self.writes.append(name)

        super().__setattr__(name, value)


def test_rollback_clears_ready_before_instance():
    """A failed construction clears `ready` before `instance`, the reverse of
    the order resolve()'s lock-free fast path reads them in."""
    container = Container()

    container.register(FailingComponent)
    definition = _WriteOrderDefinition(FailingComponent)
    container._registry[FailingComponent] = definition
    definition.writes.clear()

    with pytest.raises(RuntimeError):
        container.resolve(FailingComponent)

    assert definition.writes[-2:] == ["ready", "instance"]
    assert definition.instance is None
    assert definition.ready is False

    # The bean is genuinely disowned: resolving again rebuilds and fails again
    # instead of handing out the rolled-back instance.
    with pytest.raises(RuntimeError):
        container.resolve(FailingComponent)


def test_clear_instances_clears_ready_before_instance():
    """clear_instances() drops each singleton in the same order as rollback."""
    container = Container()

    container.register(ClearableComponent)
    definition = _WriteOrderDefinition(ClearableComponent)
    container._registry[ClearableComponent] = definition

    first = container.resolve(ClearableComponent)
    definition.writes.clear()

    container.clear_instances()

    assert definition.writes == ["ready", "instance"]
    assert container.resolve(ClearableComponent) is not first


def test_clear_instances_from_init_never_publishes_a_ready_none():
    """A bean whose __init__ clears the container drops its own cache entry,
    and the frame that built it must not then mark the emptied definition
    ready.

    `ready=True` with `instance=None` is exactly the state resolve()'s
    lock-free fast path trusts, so every later resolve() would hand back None
    cast to T -- silently, permanently, and typed as if it were the component.
    """
    container = Container()

    class SelfClearing:
        def __init__(self) -> None:
            container.clear_instances()

    container.register(SelfClearing)

    first = container.resolve(SelfClearing)
    second = container.resolve(SelfClearing)

    assert isinstance(first, SelfClearing)
    assert isinstance(second, SelfClearing)
