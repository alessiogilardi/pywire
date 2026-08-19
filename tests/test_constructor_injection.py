from typing import TYPE_CHECKING

import pytest
from pydantic_settings import BaseSettings, SettingsConfigDict

from pywire import Autowired, CircularDependencyError, Container

if TYPE_CHECKING:
    from decimal import Decimal


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


class TypeCheckingOnlyConsumer:
    """No Autowired parameters at all -- registration must not choke on the
    unresolvable `Decimal` annotation, which only exists under
    TYPE_CHECKING."""

    def __init__(self, amount: "Decimal | None" = None) -> None:
        self.amount = amount


class TypeCheckingMixedDep:
    pass


class MixedResolvableConsumer:
    """Mixes one unresolvable plain annotation with one resolvable
    Autowired[TypeCheckingMixedDep] parameter on the same __init__."""

    def __init__(
        self,
        dep: Autowired[TypeCheckingMixedDep],
        amount: "Decimal | None" = None,
    ) -> None:
        self.amount = amount
        self.dep = dep


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None)

    db_url: str = "sqlite://memory"


class SettingsConsumer:
    def __init__(self, settings: Autowired[AppSettings]) -> None:
        self.settings = settings


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


def test_circular_constructor_dependencies_raise_with_the_chain():
    """A cycle through constructor parameters has no fixed point: the
    argument must be resolved before __init__ can run. It fails naming the
    chain instead of injecting a half-constructed partner."""
    container = Container()

    container.register(CircularA)
    container.register(CircularB)

    with pytest.raises(CircularDependencyError) as excinfo:
        container.resolve(CircularA)

    assert "CircularA -> CircularB -> CircularA" in str(excinfo.value)


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


def test_type_checking_only_annotation_does_not_break_registration():
    """A class with a TYPE_CHECKING-only forward-referenced annotation on a
    non-Autowired __init__ parameter, and no Autowired parameters at all,
    must still register and resolve successfully (regression test: this
    used to raise NameError from an unconditional get_type_hints() call)."""
    container = Container()

    container.register(TypeCheckingOnlyConsumer)

    consumer = container.resolve(TypeCheckingOnlyConsumer)

    assert consumer.amount is None


def test_unresolvable_annotation_does_not_prevent_autowired_sibling():
    """A constructor mixing one unresolvable plain annotation with one
    resolvable Autowired[Dep] parameter still injects the Autowired
    parameter correctly."""
    container = Container()

    container.register(TypeCheckingMixedDep)
    container.register(MixedResolvableConsumer)

    consumer = container.resolve(MixedResolvableConsumer)
    dep = container.resolve(TypeCheckingMixedDep)

    assert consumer.dep is dep


def test_pydantic_settings_with_env_var_override(monkeypatch):
    """A pydantic BaseSettings subclass reads from environment variables
    before defaults. When an env var is set before the settings component
    is first resolved in a container, the injected settings reflect the
    env var value."""
    monkeypatch.setenv("DB_URL", "postgres://test")

    container = Container()

    container.register(AppSettings)
    container.register(SettingsConsumer)

    consumer = container.resolve(SettingsConsumer)

    assert consumer.settings.db_url == "postgres://test"


def test_pydantic_settings_with_default_value():
    """A pydantic BaseSettings subclass with a default field value is
    resolvable as a component, and can be injected into another component
    via constructor Autowired injection."""
    container = Container()

    container.register(AppSettings)
    container.register(SettingsConsumer)

    consumer = container.resolve(SettingsConsumer)

    assert consumer.settings.db_url == "sqlite://memory"
