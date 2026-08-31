"""Behavior of @pre_destroy, find_pre_destroy, and resolve_teardown.

Pure inspection: no Container involved. These tests describe what gets
discovered from a class definition alone, and how an explicit on_close
reconciles with it.
"""

import pytest

from pywire import RegistrationError
from pywire.lifecycle import find_pre_destroy, pre_destroy, resolve_teardown


def test_a_class_with_no_marked_method_has_no_teardown():
    class Plain:
        pass

    assert find_pre_destroy(Plain) is None


def test_pre_destroy_returns_the_function_unchanged():
    def close(self) -> None:
        pass

    marked = pre_destroy(close)

    assert marked is close


def test_the_marked_method_is_found_by_name():
    class Resource:
        @pre_destroy
        def shutdown(self) -> None:
            pass

    found = find_pre_destroy(Resource)

    assert found is not None
    name, func = found
    assert name == "shutdown"
    assert func is Resource.__dict__["shutdown"]


def test_a_subclass_inherits_its_base_teardown_method():
    class Base:
        @pre_destroy
        def shutdown(self) -> None:
            pass

    class Derived(Base):
        pass

    found = find_pre_destroy(Derived)

    assert found is not None
    assert found[0] == "shutdown"


def test_overriding_without_redecorating_drops_the_inherited_teardown():
    class Base:
        @pre_destroy
        def shutdown(self) -> None:
            pass

    class Derived(Base):
        def shutdown(self) -> None:  # not re-marked -- opts out
            pass

    assert find_pre_destroy(Derived) is None


def test_overriding_with_redecorating_uses_the_override():
    class Base:
        @pre_destroy
        def shutdown(self) -> None:
            pass

    class Derived(Base):
        @pre_destroy
        def shutdown(self) -> None:
            pass

    name, func = find_pre_destroy(Derived)  # type: ignore[union-operand]

    assert name == "shutdown"
    assert func is Derived.__dict__["shutdown"]


def test_two_distinct_marked_methods_are_ambiguous():
    class Broken:
        @pre_destroy
        def shutdown(self) -> None:
            pass

        @pre_destroy
        def close(self) -> None:
            pass

    with pytest.raises(RegistrationError, match="more than one"):
        find_pre_destroy(Broken)


def test_a_coroutine_pre_destroy_method_is_refused():
    class Broken:
        @pre_destroy
        async def shutdown(self) -> None:
            pass

    with pytest.raises(RegistrationError, match="coroutine"):
        find_pre_destroy(Broken)


def test_a_pre_destroy_method_requiring_extra_arguments_is_refused():
    class Broken:
        @pre_destroy
        def shutdown(self, force: bool) -> None:
            pass

    with pytest.raises(RegistrationError, match="force"):
        find_pre_destroy(Broken)


def test_a_pre_destroy_method_with_a_defaulted_extra_argument_is_fine():
    class Fine:
        @pre_destroy
        def shutdown(self, force: bool = False) -> None:
            pass

    assert find_pre_destroy(Fine) is not None


def test_resolve_teardown_returns_none_when_nothing_is_declared():
    class Plain:
        pass

    assert resolve_teardown(Plain, None) is None


def test_resolve_teardown_wraps_the_pre_destroy_method_by_name():
    calls: list[str] = []

    class Resource:
        @pre_destroy
        def shutdown(self) -> None:
            calls.append("closed")

    teardown = resolve_teardown(Resource, None)
    assert teardown is not None

    teardown(Resource())

    assert calls == ["closed"]


def test_resolve_teardown_uses_on_close_when_given():
    calls: list[object] = []

    class Plain:
        pass

    teardown = resolve_teardown(Plain, lambda instance: calls.append(instance))
    assert teardown is not None

    marker = Plain()
    teardown(marker)

    assert calls == [marker]


def test_resolve_teardown_refuses_both_pre_destroy_and_on_close():
    class Resource:
        @pre_destroy
        def shutdown(self) -> None:
            pass

    with pytest.raises(RegistrationError, match="both"):
        resolve_teardown(Resource, lambda instance: None)


def test_resolve_teardown_refuses_a_coroutine_on_close():
    class Plain:
        pass

    async def on_close(instance: object) -> None:
        pass

    with pytest.raises(RegistrationError, match="coroutine"):
        resolve_teardown(Plain, on_close)  # type: ignore[arg-type]


def test_resolve_teardown_refuses_an_on_close_with_a_required_extra_argument():
    class Plain:
        pass

    def on_close(instance: object, force: bool) -> None:
        pass

    with pytest.raises(RegistrationError, match="force"):
        resolve_teardown(Plain, on_close)  # type: ignore[arg-type]


def test_resolve_teardown_refuses_an_on_close_that_cannot_accept_the_instance():
    class Plain:
        pass

    def on_close() -> None:
        pass

    with pytest.raises(RegistrationError, match="first positional argument"):
        resolve_teardown(Plain, on_close)  # type: ignore[arg-type]


def test_resolve_teardown_tolerates_an_uninspectable_on_close(monkeypatch):
    """Best-effort: on_close may be a lambda, a bound method, or a
    C-implemented callable inspect.signature() cannot read. Deterministic via
    monkeypatch rather than hunting for a real uninspectable builtin, whose
    existence is a CPython-version implementation detail."""

    class Plain:
        pass

    def on_close(instance: object) -> None:
        pass

    def raise_type_error(func: object) -> None:
        raise TypeError("no signature found")

    monkeypatch.setattr("pywire.lifecycle.inspect.signature", raise_type_error)

    teardown = resolve_teardown(Plain, on_close)

    assert teardown is on_close
