
from pywire import Autowired, Container


class Dep:
    pass


class Consumer:
    def __init__(self, dep: Autowired[Dep]) -> None:
        self.dep = dep


class FieldDep:
    pass


class CtorDep:
    pass


class MixedConsumer:
    field_dep: Autowired[FieldDep]

    def __init__(self, ctor_dep: Autowired[CtorDep]) -> None:
        self.ctor_dep = ctor_dep


class CircularA:
    def __init__(self, b: Autowired["CircularB"]) -> None:
        self.b = b


class CircularB:
    def __init__(self, a: Autowired[CircularA]) -> None:
        self.a = a


class ForwardRefConsumer:
    def __init__(self, other: Autowired["ForwardRefTarget"]) -> None:
        self.other = other


class ForwardRefTarget:
    pass


class NotInjected:
    pass


class PlainDefaultConsumer:
    def __init__(self, value: NotInjected | None = None) -> None:
        self.value = value


def test_constructor_parameter_is_resolved():
    """A required Autowired[T] constructor parameter is auto-resolved."""
    container = Container()

    container.register(Dep)
    container.register(Consumer)

    consumer = container.resolve(Consumer)
    dep = container.resolve(Dep)

    assert consumer.dep is dep


def test_field_and_constructor_injection_coexist():
    """A class-level Autowired field and a constructor Autowired parameter
    both resolve correctly and independently on the same class."""
    container = Container()

    container.register(FieldDep)
    container.register(CtorDep)
    container.register(MixedConsumer)

    consumer = container.resolve(MixedConsumer)

    assert consumer.field_dep is container.resolve(FieldDep)
    assert consumer.ctor_dep is container.resolve(CtorDep)


def test_circular_dependency_via_constructor_injection():
    """Circular dependencies wired through constructor parameters resolve
    without infinite recursion, and both sides reference each other's
    final instance once fully resolved."""
    container = Container()

    container.register(CircularA)
    container.register(CircularB)

    a = container.resolve(CircularA)
    b = container.resolve(CircularB)

    assert a.b is b
    assert b.a is a


def test_forward_reference_constructor_parameter():
    """A forward-reference string constructor parameter (Autowired["Other"])
    resolves correctly once the referenced class is defined later in the
    module."""
    container = Container()

    container.register(ForwardRefTarget)
    container.register(ForwardRefConsumer)

    consumer = container.resolve(ForwardRefConsumer)
    target = container.resolve(ForwardRefTarget)

    assert consumer.other is target


def test_non_autowired_default_parameter_is_left_untouched():
    """A constructor parameter with a default value that is NOT annotated
    Autowired[...] is left alone: no injection is attempted."""
    container = Container()

    container.register(PlainDefaultConsumer)

    consumer = container.resolve(PlainDefaultConsumer)

    assert consumer.value is None
