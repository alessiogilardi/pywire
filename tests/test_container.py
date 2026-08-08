
from pywire import Autowired, Container, component, get_global_container


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


def test_global_container_functionality():
    """Test the global container functionality."""
    global_container = get_global_container()

    assert global_container is not None
    assert isinstance(global_container, Container)

    # Get it again to verify singleton behavior
    same_container = get_global_container()
    assert same_container is global_container
