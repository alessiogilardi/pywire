"""Behavior of register_factory and register_instance.

These tests describe the provider model from the outside: what a caller
observes, never how the registry stores it.
"""

import threading
from typing import Protocol

import pytest

from pywire import Autowired, Container, DependencyResolutionError, RegistrationError


class Engine:
    """Stand-in for a third-party object that is not zero-arg constructible."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn


def test_factory_is_not_called_until_something_resolves():
    container = Container()
    calls: list[int] = []

    def make_engine() -> Engine:
        calls.append(1)
        return Engine("postgres://")

    container.register_factory(Engine, make_engine)

    assert calls == []

    container.resolve(Engine)

    assert calls == [1]


def test_factory_is_called_once_and_its_result_is_the_singleton():
    container = Container()
    calls: list[int] = []

    def make_engine() -> Engine:
        calls.append(1)
        return Engine("postgres://")

    container.register_factory(Engine, make_engine)

    first = container.resolve(Engine)
    second = container.resolve(Engine)

    assert first is second
    assert calls == [1]


def test_factory_may_resolve_its_own_dependencies():
    container = Container()

    class Settings:
        dsn = "postgres://from-settings"

    container.register(Settings)
    container.register_factory(
        Engine, lambda: Engine(container.resolve(Settings).dsn)
    )

    assert container.resolve(Engine).dsn == "postgres://from-settings"


def test_factory_returning_none_is_refused():
    container = Container()

    container.register_factory(Engine, lambda: None)  # type: ignore[arg-type,return-value]

    with pytest.raises(DependencyResolutionError, match="returned None"):
        container.resolve(Engine)


def test_async_factory_is_refused_at_registration():
    container = Container()

    async def make_engine() -> Engine:
        return Engine("postgres://")

    with pytest.raises(RegistrationError, match="coroutine function"):
        container.register_factory(Engine, make_engine)  # type: ignore[arg-type]


def test_registering_a_factory_for_a_taken_key_is_refused():
    container = Container()

    container.register_factory(Engine, lambda: Engine("a"))

    with pytest.raises(RegistrationError, match="is already registered"):
        container.register_factory(Engine, lambda: Engine("b"))


def test_factory_bean_is_rebuilt_after_clear_instances():
    container = Container()

    container.register_factory(Engine, lambda: Engine("postgres://"))

    first = container.resolve(Engine)
    container.clear_instances()
    second = container.resolve(Engine)

    assert first is not second


def test_a_failing_factory_leaves_nothing_cached_and_can_be_retried():
    container = Container()
    attempts: list[int] = []

    def flaky() -> Engine:
        attempts.append(1)

        if len(attempts) == 1:
            raise RuntimeError("boom")

        return Engine("postgres://")

    container.register_factory(Engine, flaky)

    with pytest.raises(RuntimeError, match="boom"):
        container.resolve(Engine)

    assert container.resolve(Engine).dsn == "postgres://"
    assert attempts == [1, 1]


def test_a_factory_bean_is_rolled_back_when_an_upstream_frame_fails():
    """Rollback is per-subtree, so a factory bean built inside a failing branch
    must not stay cached -- and must be rebuilt, not resurrected, afterwards."""
    container = Container()
    built: list[Engine] = []

    def make_engine() -> Engine:
        engine = Engine("postgres://")
        built.append(engine)
        return engine

    class Repo:
        engine: Autowired[Engine]

        def __init__(self) -> None:
            raise RuntimeError("upstream boom")

    container.register_factory(Engine, make_engine)
    container.register(Repo)

    with pytest.raises(RuntimeError, match="upstream boom"):
        container.resolve(Repo)

    second = container.resolve(Engine)

    assert len(built) == 2
    assert second is built[1]


def test_concurrent_resolution_calls_the_factory_once():
    """A widened window, not a race we hope to lose.

    The factory sleeps so every thread is inside resolve() while the first one
    is still building; a bare barrier reproduces a missing lock only about one
    run in six, which is too flaky to protect anything.
    """
    container = Container()
    calls: list[int] = []

    def slow_engine() -> Engine:
        calls.append(1)
        threading.Event().wait(0.05)
        return Engine("postgres://")

    container.register_factory(Engine, slow_engine)

    results: list[Engine] = []
    threads = [
        threading.Thread(target=lambda: results.append(container.resolve(Engine)))
        for _ in range(8)
    ]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join()

    assert calls == [1]
    assert len({id(result) for result in results}) == 1


class PostgresConfig:
    def __init__(self, host: str) -> None:
        self.host = host


class AppConfig:
    def __init__(self) -> None:
        self.postgres = PostgresConfig("db.internal")


def test_a_pushed_instance_is_returned_by_identity():
    container = Container()
    config = AppConfig()

    container.register_instance(config)

    assert container.resolve(AppConfig) is config


def test_the_default_key_is_the_runtime_type():
    container = Container()
    config = AppConfig()

    container.register_instance(config.postgres)

    assert container.resolve(PostgresConfig) is config.postgres


def test_pushing_none_is_refused():
    container = Container()

    with pytest.raises(RegistrationError, match="Cannot register None"):
        container.register_instance(None)


def test_a_pushed_instance_survives_clear_instances_by_identity():
    """The payoff of instance-as-factory: teardown stays uniform.

    A pushed object is not reconstructible by the container, so a design that
    dropped it on clear would leave a definition nobody can repopulate. The
    closure makes rebuilding it mean handing back the same object.
    """
    container = Container()
    config = AppConfig()

    container.register_instance(config)

    first = container.resolve(AppConfig)
    container.clear_instances()

    assert container.resolve(AppConfig) is first


def test_a_pushed_instance_is_not_wired():
    """The trap of this API, written down so it cannot surprise anyone.

    The container injects only into objects it constructs. This one arrived
    already built, so its Autowired field is simply absent.
    """
    container = Container()

    class Dependency:
        pass

    class Service:
        dep: Autowired[Dependency]

    container.register(Dependency)
    container.register_instance(Service())

    assert not hasattr(container.resolve(Service), "dep")


def test_pushing_over_a_taken_key_is_refused():
    container = Container()

    container.register_instance(AppConfig())

    with pytest.raises(RegistrationError, match="is already registered"):
        container.register_instance(AppConfig())


class UserRepository(Protocol):
    def name(self) -> str: ...


class PostgresUserRepo:
    def name(self) -> str:
        return "postgres"


def test_a_class_bound_to_a_supertype_resolves_under_that_supertype():
    container = Container()

    container.register(PostgresUserRepo, as_type=UserRepository)

    assert isinstance(container.resolve(UserRepository), PostgresUserRepo)


def test_as_type_rebinds_and_does_not_add():
    """The concrete type is no longer a key: consumers must use the abstraction."""
    container = Container()

    container.register(PostgresUserRepo, as_type=UserRepository)

    with pytest.raises(DependencyResolutionError, match="not registered"):
        container.resolve(PostgresUserRepo)


def test_a_rebound_class_is_still_built_and_wired_by_the_container():
    container = Container()

    class Dependency:
        pass

    class Repo:
        dep: Autowired[Dependency]

        def name(self) -> str:
            return "repo"

    container.register(Dependency)
    container.register(Repo, as_type=UserRepository)

    resolved = container.resolve(UserRepository)

    assert isinstance(resolved, Repo)
    assert resolved.dep is container.resolve(Dependency)


def test_an_autowired_field_resolves_through_the_supertype():
    container = Container()

    class Service:
        repo: Autowired[UserRepository]

    container.register(PostgresUserRepo, as_type=UserRepository)
    container.register(Service)

    assert container.resolve(Service).repo.name() == "postgres"


def test_a_pushed_instance_can_be_bound_to_a_supertype():
    container = Container()
    repo = PostgresUserRepo()

    container.register_instance(repo, as_type=UserRepository)

    assert container.resolve(UserRepository) is repo


def test_two_implementations_cannot_claim_the_same_supertype():
    container = Container()

    class OtherRepo:
        def name(self) -> str:
            return "other"

    container.register(PostgresUserRepo, as_type=UserRepository)

    with pytest.raises(RegistrationError, match="is already registered"):
        container.register(OtherRepo, as_type=UserRepository)
