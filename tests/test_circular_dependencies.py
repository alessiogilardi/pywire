
from pywire import Autowired, Container


class CircularServiceA:
    service_b: Autowired["CircularServiceB"]


class CircularServiceB:
    service_a: Autowired[CircularServiceA]


class ServiceX:
    service_y: Autowired["ServiceY"]


class ServiceY:
    service_x: Autowired[ServiceX]


class CycleEntryConsumer:
    """Enters the ServiceX/ServiceY field cycle through a constructor
    parameter -- the shape a cycle scan that starts at the entry frame
    misreads as a constructor cycle."""

    def __init__(self, service_x: Autowired[ServiceX]) -> None:
        self.service_x = service_x


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


def test_field_cycle_reached_through_an_unrelated_constructor_edge():
    """A field cycle stays legal when it is entered through a constructor
    parameter: the edge that led *into* the cycle is not one of the cycle's
    own edges. Rejecting it would make the same object graph resolve or fail
    depending on which type happened to be resolved first."""
    container = Container()

    container.register(ServiceX)
    container.register(ServiceY)
    container.register(CycleEntryConsumer)

    consumer = container.resolve(CycleEntryConsumer)

    assert consumer.service_x is container.resolve(ServiceX)
    assert consumer.service_x.service_y is container.resolve(ServiceY)
    assert consumer.service_x.service_y.service_x is consumer.service_x
