"""Behavior of Container.close() and the context manager protocol."""

import threading

import pytest

from pywire import Autowired, Container


def test_close_calls_on_close_for_a_resolved_bean():
    container = Container()
    calls: list[str] = []

    class Resource:
        pass

    container.register(Resource, on_close=lambda instance: calls.append("closed"))
    container.resolve(Resource)

    container.close()

    assert calls == ["closed"]


def test_close_skips_a_bean_that_was_never_resolved():
    container = Container()
    calls: list[str] = []

    class Resource:
        pass

    container.register(Resource, on_close=lambda instance: calls.append("closed"))

    container.close()

    assert calls == []


def test_close_tears_down_in_reverse_ready_order():
    container = Container()
    order: list[str] = []

    class Dep:
        pass

    class Service:
        dep: Autowired[Dep]

    container.register(Dep, on_close=lambda instance: order.append("dep"))
    container.register(Service, on_close=lambda instance: order.append("service"))

    container.resolve(Service)  # builds Dep first, then Service

    container.close()

    assert order == ["service", "dep"]


def test_close_attempts_every_bean_even_if_one_fails():
    container = Container()
    calls: list[str] = []

    class A:
        pass

    class B:
        pass

    def fail(instance: object) -> None:
        raise RuntimeError("boom")

    container.register(A, on_close=fail)
    container.register(B, on_close=lambda instance: calls.append("b"))

    container.resolve(A)
    container.resolve(B)

    with pytest.raises(ExceptionGroup) as exc_info:
        container.close()

    assert calls == ["b"]
    assert len(exc_info.value.exceptions) == 1
    assert isinstance(exc_info.value.exceptions[0], RuntimeError)


def test_close_leaves_the_container_reusable():
    container = Container()

    class Resource:
        pass

    container.register(Resource)
    first = container.resolve(Resource)

    container.close()

    second = container.resolve(Resource)

    assert second is not first


def test_a_second_close_is_a_no_op():
    container = Container()
    calls: list[str] = []

    class Resource:
        pass

    container.register(Resource, on_close=lambda instance: calls.append("closed"))
    container.resolve(Resource)

    container.close()
    container.close()

    assert calls == ["closed"]


def test_context_manager_closes_on_normal_exit():
    calls: list[str] = []

    class Resource:
        pass

    with Container() as container:
        container.register(Resource, on_close=lambda instance: calls.append("closed"))
        container.resolve(Resource)

    assert calls == ["closed"]


def test_context_manager_closes_even_when_the_body_raises():
    calls: list[str] = []

    class Resource:
        pass

    with pytest.raises(RuntimeError, match="boom"):
        with Container() as container:
            container.register(
                Resource, on_close=lambda instance: calls.append("closed")
            )
            container.resolve(Resource)
            raise RuntimeError("boom")

    assert calls == ["closed"]


def test_a_pushed_instance_with_on_close_is_lazy_like_every_other_bean():
    container = Container()
    calls: list[str] = []

    class Resource:
        pass

    container.register_instance(
        Resource(), on_close=lambda instance: calls.append("closed")
    )
    # Deliberately never resolved.

    container.close()

    assert calls == []


def test_close_does_not_block_a_concurrent_resolve_on_another_type():
    """close()'s lock is released before teardown runs, so a slow teardown
    must not stall an unrelated resolve() on another thread."""
    container = Container()
    teardown_started = threading.Event()
    release_teardown = threading.Event()
    fast_resolved: list[object] = []

    class Slow:
        pass

    class Fast:
        pass

    def slow_close(instance: object) -> None:
        teardown_started.set()
        release_teardown.wait(timeout=2)

    container.register(Slow, on_close=slow_close)
    container.register(Fast)
    container.resolve(Slow)

    close_thread = threading.Thread(target=container.close)
    close_thread.start()

    assert teardown_started.wait(timeout=2)

    # Run on its own thread with a bounded join, not called directly here: if
    # close() still held the lock, a direct call would hang this test (and
    # the whole suite) indefinitely instead of failing cleanly.
    resolve_thread = threading.Thread(
        target=lambda: fast_resolved.append(container.resolve(Fast))
    )
    resolve_thread.start()
    resolve_thread.join(timeout=2)

    assert not resolve_thread.is_alive()
    assert len(fast_resolved) == 1
    assert isinstance(fast_resolved[0], Fast)

    release_teardown.set()
    close_thread.join(timeout=2)

    assert not close_thread.is_alive()
