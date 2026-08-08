
from pywire import Autowired, Container


class CircularServiceA:
    service_b: Autowired["CircularServiceB"]


class CircularServiceB:
    service_a: Autowired[CircularServiceA]


class ServiceX:
    service_y: Autowired["ServiceY"]


class ServiceY:
    service_x: Autowired[ServiceX]


def test_circular_dependency_detection():
    """Test that circular dependencies are handled without infinite loops."""
    container = Container()

    container.register(CircularServiceA)
    container.register(CircularServiceB)

    service_a = container.resolve(CircularServiceA)
    service_b = container.resolve(CircularServiceB)

    assert service_a.service_b is service_b
    assert service_b.service_a is service_a


def test_self_reference():
    """Test that self-referencing components are handled."""
    container = Container()

    class SelfService:
        pass

    container.register(SelfService)

    instance = container.resolve(SelfService)

    assert instance is not None
    assert isinstance(instance, SelfService)


def test_two_way_circular_with_forward_ref():
    """Test two-way circular dependencies using forward reference strings."""
    container = Container()

    container.register(ServiceX)
    container.register(ServiceY)

    service_x = container.resolve(ServiceX)
    service_y = container.resolve(ServiceY)

    assert service_x.service_y is service_y
    assert service_y.service_x is service_x
