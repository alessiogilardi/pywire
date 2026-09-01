import functools
import importlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Protocol

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pydantic_settings import BaseSettings, SettingsConfigDict

import pywire.fastapi
from pywire import (
    AnnotationResolutionError,
    Autowired,
    Container,
    component,
    pre_destroy,
)
from pywire.fastapi import pywire_lifespan, wire


class PreexistingRouterRepo:
    def __init__(self) -> None:
        self.value = "preexisting-repo-value"


class PreexistingRouterService:
    repo: Autowired[PreexistingRouterRepo]


# Defined at true module scope, before any test function runs and before any
# FastAPI() instance exists anywhere in this process -- the real-world
# pattern the redesign targets: an APIRouter built and decorated in its own
# module, at import time, with no FastAPI app in sight yet.
router_defined_before_any_app = APIRouter()


@router_defined_before_any_app.get("/preexisting")
def get_preexisting(service: Autowired[PreexistingRouterService]) -> dict:
    return {"value": service.repo.value}


class Repo:
    def __init__(self) -> None:
        self.value = "repo-value"


class Service:
    repo: Autowired[Repo]

    def __init__(self) -> None:
        self.calls = 0

    def call(self) -> int:
        self.calls += 1
        return self.calls


def test_route_reuses_same_singleton_across_requests():
    """Two separate requests to a wired route both use the same singleton
    Service instance, proven by an in-memory counter that keeps
    incrementing across requests."""
    container = Container()
    container.register(Repo)
    container.register(Service)

    app = FastAPI()
    wire(app, container=container)

    @app.get("/calls")
    def get_calls(service: Autowired[Service]) -> dict:
        return {"calls": service.call()}

    client = TestClient(app)

    first = client.get("/calls")
    second = client.get("/calls")

    assert first.json() == {"calls": 1}
    assert second.json() == {"calls": 2}


def test_route_reaches_nested_field_injection():
    """The injected Service has its own Autowired[Repo] class-level field;
    the route can reach through to service.repo data via the response."""
    container = Container()
    container.register(Repo)
    container.register(Service)

    app = FastAPI()
    wire(app, container=container)

    @app.get("/repo-value")
    def get_repo_value(service: Autowired[Service]) -> dict:
        return {"repo_value": service.repo.value}

    client = TestClient(app)

    response = client.get("/repo-value")

    assert response.json() == {"repo_value": "repo-value"}


class ForwardRefService:
    def __init__(self) -> None:
        self.marker = "forward-ref-ok"


def test_route_resolves_forward_reference_string():
    """A route parameter using Autowired["ForwardRefService"] (a
    forward-reference string) resolves correctly."""
    container = Container()
    container.register(ForwardRefService)

    app = FastAPI()
    wire(app, container=container)

    @app.get("/forward-ref")
    def get_forward_ref(service: Autowired["ForwardRefService"]) -> dict:
        return {"marker": service.marker}

    client = TestClient(app)

    response = client.get("/forward-ref")

    assert response.json() == {"marker": "forward-ref-ok"}


class CustomContainerService:
    def __init__(self) -> None:
        self.origin = "custom-container"


def test_wire_uses_explicit_container_instead_of_default():
    """An explicit container= passed to wire() is used instead of the
    global default container: the default container's registry does not
    contain the component, only the explicit one does."""
    from pywire.decorators import get_default_container

    my_container = Container()
    my_container.register(CustomContainerService)

    app = FastAPI()
    wire(app, container=my_container)

    @app.get("/origin")
    def get_origin(service: Autowired[CustomContainerService]) -> dict:
        return {"origin": service.origin}

    client = TestClient(app)

    response = client.get("/origin")

    assert response.json() == {"origin": "custom-container"}
    assert CustomContainerService not in get_default_container()._registry
    assert CustomContainerService in my_container._registry


class RouterRepo:
    def __init__(self) -> None:
        self.value = "router-repo-value"


class RouterService:
    """Dedicated classes for the router-decoration test below (rather than
    reusing Repo/Service) purely for test-case clarity: keeping each test's
    component classes distinct makes assertions unambiguous about which
    container and which instance produced a given value."""

    repo: Autowired[RouterRepo]

    def __init__(self) -> None:
        self.calls = 0

    def call(self) -> int:
        self.calls += 1
        return self.calls


def test_wire_router_before_decoration_supports_include_router_pattern():
    """Real-world pattern: routes are declared on a per-module APIRouter via
    decorators, then the router is mounted onto the app with
    include_router(). The router is built and its route decorated with no
    wire() call in sight -- wire() only ever runs on the FastAPI app, after
    the router already exists, before include_router() mounts it. The
    global add_api_route patch is what makes this safe: the route's bare
    Autowired[T] parameter was already rewritten into Depends(...) at
    decoration time, on the router itself, regardless of wire() having run
    yet for anything."""
    container = Container()
    container.register(RouterRepo)
    container.register(RouterService)

    router = APIRouter()

    @router.get("/calls")
    def get_calls(service: Autowired[RouterService]) -> dict:
        return {"calls": service.call()}

    app = FastAPI()
    wire(app, container=container)
    app.include_router(router)

    client = TestClient(app)

    response = client.get("/calls")

    assert response.json() == {"calls": 1}


def test_route_decorated_before_fastapi_app_exists_still_resolves():
    """A route decorated on a bare APIRouter before any FastAPI() instance
    exists anywhere (see router_defined_before_any_app at module scope,
    above) still resolves correctly once an app is later created, wired,
    and the router is included. This is the exact scenario that raised
    FastAPIError at import time under the old route_class mechanism."""
    container = Container()
    container.register(PreexistingRouterRepo)
    container.register(PreexistingRouterService)

    app = FastAPI()
    wire(app, container=container)
    app.include_router(router_defined_before_any_app)

    client = TestClient(app)

    response = client.get("/preexisting")

    assert response.json() == {"value": "preexisting-repo-value"}


@component
class DefaultContainerRepo:
    def __init__(self) -> None:
        self.value = "default-container-value"


def test_autowired_resolves_via_default_container_when_wire_never_called():
    """If wire() is never called for an app at all, Autowired[T] route
    parameters still resolve -- against the module-level default container,
    the same one @component registers into."""
    app = FastAPI()

    @app.get("/default")
    def get_default(repo: Autowired[DefaultContainerRepo]) -> dict:
        return {"value": repo.value}

    client = TestClient(app)

    response = client.get("/default")

    assert response.json() == {"value": "default-container-value"}


def test_wire_rejects_invalid_target():
    """wire() only accepts a FastAPI instance; anything else -- including a
    bare APIRouter, which is no longer a supported target now that routing
    is patched globally -- raises TypeError instead of failing later with
    an unhelpful AttributeError."""
    with pytest.raises(TypeError, match="FastAPI instance"):
        wire(object())  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="FastAPI instance"):
        wire(APIRouter())  # type: ignore[arg-type]


class AppConfig(BaseSettings):
    """A base config, resolved once and injected wherever a component
    declares Autowired[AppConfig] -- field, constructor, or route parameter."""

    model_config = SettingsConfigDict(env_file=None)

    api_prefix: str = "/api"
    db_url: str = "sqlite://memory"
    request_timeout: float = 5.0


class ConfigConsumingRepository:
    config: Autowired[AppConfig]


class ConfigConsumingService:
    config: Autowired[AppConfig]
    repository: Autowired[ConfigConsumingRepository]


def test_app_config_is_shared_across_services_and_repositories(monkeypatch):
    """A single AppConfig (BaseSettings) instance, registered once, is the
    exact same object reached through every path a wired app can reach it:
    a route's own Autowired[AppConfig] parameter, a Service's own config
    field, and a Repository's config field reached transitively through the
    Service. Proves the config is truly *shared*, not re-read/re-instantiated
    per component."""
    monkeypatch.setenv("API_PREFIX", "/v2")

    container = Container()
    container.register(AppConfig)
    container.register(ConfigConsumingRepository)
    container.register(ConfigConsumingService)

    app = FastAPI()
    wire(app, container=container)

    @app.get("/describe")
    def describe(
        service: Autowired[ConfigConsumingService],
        config: Autowired[AppConfig],
    ) -> dict:
        return {
            "route_prefix": config.api_prefix,
            "service_timeout": service.config.request_timeout,
            "repository_db_url": service.repository.config.db_url,
            "route_config_is_service_config": service.config is config,
            "service_config_is_repository_config": (
                service.repository.config is service.config
            ),
        }

    client = TestClient(app)

    response = client.get("/describe")
    data = response.json()

    expected = container.resolve(AppConfig)
    assert data["route_prefix"] == "/v2" == expected.api_prefix
    assert data["service_timeout"] == expected.request_timeout
    assert data["repository_db_url"] == expected.db_url
    assert data["route_config_is_service_config"] is True
    assert data["service_config_is_repository_config"] is True


def test_install_patch_does_not_double_wrap_on_module_reload():
    """Re-running pywire.fastapi's module body (e.g. via importlib.reload)
    must not wrap an already-patched APIRouter.add_api_route a second time
    -- the guard inside _install_patch() is what makes this safe."""
    patched_before_reload = APIRouter.add_api_route

    importlib.reload(pywire.fastapi)

    assert APIRouter.add_api_route is patched_before_reload


def _partial_target(a: int) -> dict:
    return {"a": a}


def test_functools_partial_endpoint_is_not_broken_by_the_patch():
    """A functools.partial endpoint (a supported FastAPI pattern, unrelated
    to Autowired[T]) must still register correctly through the globally
    patched add_api_route -- _wire_endpoint must not choke on non-function
    callables it was never meant to inspect."""
    app = FastAPI()

    app.add_api_route(
        "/partial", functools.partial(_partial_target, a=2), methods=["GET"]
    )

    client = TestClient(app)

    response = client.get("/partial")

    assert response.json() == {"a": 2}


class TargetA:
    def __init__(self) -> None:
        self.origin = "container-a"


class TargetB:
    def __init__(self) -> None:
        self.origin = "container-b"


def test_two_apps_resolve_independently_via_their_own_wired_container():
    """request.app.state.pywire_container means each app's routes resolve
    against that specific app's container -- two apps in the same process,
    each wired with a different container, must not cross-contaminate."""
    container_a = Container()
    container_a.register(TargetA)

    container_b = Container()
    container_b.register(TargetB)

    app_a = FastAPI()
    wire(app_a, container=container_a)

    app_b = FastAPI()
    wire(app_b, container=container_b)

    @app_a.get("/origin")
    def get_origin_a(target: Autowired[TargetA]) -> dict:
        return {"origin": target.origin}

    @app_b.get("/origin")
    def get_origin_b(target: Autowired[TargetB]) -> dict:
        return {"origin": target.origin}

    client_a = TestClient(app_a)
    client_b = TestClient(app_b)

    response_a = client_a.get("/origin")
    response_b = client_b.get("/origin")

    assert response_a.json() == {"origin": "container-a"}
    assert response_b.json() == {"origin": "container-b"}


# Decorated at *import* time, above the service it injects -- which is the only
# shape that actually reaches the deferred path. A route decorated inside a test
# body runs after the module has finished importing, so its annotation resolves
# immediately and the deferred path is never exercised. app.get() goes straight
# to app.router.add_api_route, so the endpoint is wired exactly once; do not
# rewrite this as an APIRouter + include_router, which wires it twice.
_late_app = FastAPI()


@_late_app.get("/late")
def _late_endpoint(service: Autowired["LateDefinedService"]) -> dict[str, str]:
    return {"value": service.value()}


class LateDefinedService:
    """Defined below the route that injects it, on purpose."""

    def value(self) -> str:
        return "late"


def test_endpoint_can_inject_a_service_defined_below_it() -> None:
    """Decoration must not require the injected type to exist yet -- the
    container's own planning is lazy for exactly this reason."""
    container = Container()
    container.register(LateDefinedService)

    wire(_late_app, container=container)

    response = TestClient(_late_app).get("/late")

    assert response.status_code == 200
    assert response.json() == {"value": "late"}


def test_unresolvable_autowired_parameter_fails_at_request_time() -> None:
    """A genuinely broken annotation must not break decoration; it fails on the
    first request, with a pywire error naming the endpoint."""
    app = FastAPI()

    @app.get("/broken")
    def broken(
        service: Autowired["NoSuchService"],  # noqa: F821  # pyright: ignore[reportUndefinedVariable]
    ) -> dict[str, str]:
        return {"ok": "yes"}

    with pytest.raises(AnnotationResolutionError) as excinfo:
        TestClient(app).get("/broken")

    assert "broken" in str(excinfo.value)


def test_decoration_tolerates_an_unrelated_unresolvable_annotation() -> None:
    """A parameter annotation that is not Autowired[...] at all, and cannot be
    resolved (a TYPE_CHECKING-only import, say), must not abort route
    decoration. Before this task, _wire_endpoint called get_type_hints()
    directly: one unresolvable annotation anywhere on the endpoint raised
    NameError and aborted add_api_route for the whole route. callable_hints()
    tolerates it per-annotation instead, via a fallback distinct from the
    Autowired-raises-then-defers branch exercised by the two tests above --
    this parameter is never wrapped in Autowired[...], so resolve_autowired_
    type never raises for it; it simply resolves to None and is left alone."""
    app = FastAPI()

    @app.get("/unrelated")
    def endpoint(
        x: "SomeUndefinedName" = None,  # noqa: F821  # pyright: ignore[reportUndefinedVariable]
    ) -> dict[str, str]:
        return {"ok": "yes"}

    response = TestClient(app).get("/unrelated")

    assert response.status_code == 200
    assert response.json() == {"ok": "yes"}


def test_a_protocol_bound_dependency_resolves_in_a_route():
    """as_type makes a route parameter's annotation a Protocol.

    _wire_endpoint rewrites it to `annotation=target` plus Depends(...), and
    FastAPI never validates the annotation of a Depends-defaulted parameter --
    verified against this project's FastAPI before the design was written.
    """
    container = Container()

    class Greeter(Protocol):
        def greet(self) -> str: ...

    class ItalianGreeter:
        def greet(self) -> str:
            return "ciao"

    container.register(ItalianGreeter, as_type=Greeter)

    app = FastAPI()
    wire(app, container=container)

    @app.get("/greet")
    def greet_route(greeter: Autowired[Greeter]) -> dict[str, str]:
        return {"greeting": greeter.greet()}

    response = TestClient(app).get("/greet")

    assert response.status_code == 200
    assert response.json() == {"greeting": "ciao"}


def test_lifespan_rejects_a_positional_that_is_not_an_app():
    """pywire_lifespan(container) -- the keyword forgotten -- must say so,
    not fail later as an AttributeError on Container.state."""
    container = Container()

    with pytest.raises(TypeError, match="Container"):
        pywire_lifespan(container)  # type: ignore[arg-type]


def test_lifespan_rejects_an_app_and_configuration_together():
    """Not reachable through either overload. Running it would ignore
    container= and bind the default container instead -- silently."""
    app = FastAPI()
    container = Container()

    with pytest.raises(TypeError, match="cannot take both"):
        pywire_lifespan(app, container=container)  # type: ignore[call-overload]


def test_lifespan_rejects_an_app_and_close_on_shutdown_together():
    """Same guard as above, triggered by close_on_shutdown instead of
    container -- both are configuration that an app-bound call would
    silently ignore."""
    app = FastAPI()

    with pytest.raises(TypeError, match="cannot take both"):
        pywire_lifespan(app, close_on_shutdown=False)  # type: ignore[call-overload]


def test_two_different_containers_configured_for_one_app_is_rejected():
    """wire(app, container=A) plus pywire_lifespan(container=B): one of the
    two is dead configuration and its beans would never be closed."""

    class ConflictService:
        pass

    first = Container()
    first.register(ConflictService)
    second = Container()
    second.register(ConflictService)

    app = FastAPI(lifespan=pywire_lifespan(container=second))
    wire(app, container=first)

    with pytest.raises(RuntimeError, match="already bound"):
        with TestClient(app):
            pass


def test_the_same_container_configured_twice_is_accepted():
    """Redundant, not contradictory: wire() and the lifespan naming the
    same object is harmless and must not raise."""

    class RedundantService:
        def __init__(self) -> None:
            self.origin = "redundant"

    container = Container()
    container.register(RedundantService)

    app = FastAPI(lifespan=pywire_lifespan(container=container))
    wire(app, container=container)

    @app.get("/origin")
    def get_origin(service: Autowired[RedundantService]) -> dict:
        return {"origin": service.origin}

    with TestClient(app) as client:
        assert client.get("/origin").json() == {"origin": "redundant"}


def test_pre_destroy_runs_when_the_app_shuts_down():
    """The whole point: a bean resolved while serving requests gets its
    teardown called when the ASGI lifespan ends."""
    log: list[str] = []

    class Pool:
        @pre_destroy
        def shutdown(self) -> None:
            log.append("pool")

    container = Container()
    container.register(Pool)

    app = FastAPI(lifespan=pywire_lifespan(container=container))

    @app.get("/ping")
    def ping(pool: Autowired[Pool]) -> dict:
        return {"ok": True}

    with TestClient(app) as client:
        assert client.get("/ping").status_code == 200
        assert log == []

    assert log == ["pool"]


def test_shutdown_tears_dependents_down_before_dependencies():
    """close()'s reverse-ready ordering reaches through the lifespan
    unchanged: the service that depends on the pool is closed first."""
    log: list[str] = []

    class Pool:
        @pre_destroy
        def shutdown(self) -> None:
            log.append("pool")

    class Users:
        pool: Autowired[Pool]

        @pre_destroy
        def shutdown(self) -> None:
            log.append("users")

    container = Container()
    container.register(Pool)
    container.register(Users)

    app = FastAPI(lifespan=pywire_lifespan(container=container))

    @app.get("/users")
    def list_users(users: Autowired[Users]) -> dict:
        return {"ok": True}

    with TestClient(app) as client:
        client.get("/users")

    assert log == ["users", "pool"]


def test_close_on_shutdown_false_binds_without_tearing_down():
    """The escape hatch for a container shared by more than one app:
    routes still resolve, nothing is closed."""
    log: list[str] = []

    class Pool:
        @pre_destroy
        def shutdown(self) -> None:
            log.append("pool")

    container = Container()
    container.register(Pool)

    app = FastAPI(
        lifespan=pywire_lifespan(container=container, close_on_shutdown=False)
    )

    @app.get("/ping")
    def ping(pool: Autowired[Pool]) -> dict:
        return {"ok": True}

    with TestClient(app) as client:
        assert client.get("/ping").status_code == 200

    assert log == []


def test_bare_form_binds_and_closes_the_default_container():
    """FastAPI(lifespan=pywire_lifespan), no parentheses: the module-level
    default container -- the one @component writes into -- is bound and
    closed."""
    log: list[str] = []

    @component
    class DefaultPool:
        @pre_destroy
        def shutdown(self) -> None:
            log.append("default-pool")

    app = FastAPI(lifespan=pywire_lifespan)

    @app.get("/ping")
    def ping(pool: Autowired[DefaultPool]) -> dict:
        return {"ok": True}

    with TestClient(app) as client:
        assert client.get("/ping").status_code == 200

    assert log == ["default-pool"]


def test_empty_parentheses_behave_like_the_bare_form():
    """pywire_lifespan() is legal -- unlike component(), nothing mandatory
    is missing -- and means the same as the bare form."""
    log: list[str] = []

    @component
    class EmptyParensPool:
        @pre_destroy
        def shutdown(self) -> None:
            log.append("empty-parens-pool")

    app = FastAPI(lifespan=pywire_lifespan())

    @app.get("/ping")
    def ping(pool: Autowired[EmptyParensPool]) -> dict:
        return {"ok": True}

    with TestClient(app) as client:
        assert client.get("/ping").status_code == 200

    assert log == ["empty-parens-pool"]


def test_an_explicit_container_is_used_instead_of_the_default_one():
    """container= wins over the default container, and the app.state
    binding the routes read at request time points at it."""

    class ScopedService:
        def __init__(self) -> None:
            self.origin = "explicit-container"

    container = Container()
    container.register(ScopedService)

    app = FastAPI(lifespan=pywire_lifespan(container=container))

    @app.get("/origin")
    def get_origin(service: Autowired[ScopedService]) -> dict:
        return {"origin": service.origin}

    with TestClient(app) as client:
        response = client.get("/origin")

    assert response.json() == {"origin": "explicit-container"}
    assert app.state.pywire_container is container


def _teardown_leaves(error: BaseException) -> list[BaseException]:
    """Flatten nested exception groups down to their leaf exceptions.

    close() raises an ExceptionGroup, and starlette/anyio may wrap what
    propagates out of the lifespan in a further group. Asserting on the
    leaves keeps these tests independent of how many layers of grouping
    happen to be in play.
    """
    if isinstance(error, BaseExceptionGroup):
        return [leaf for sub in error.exceptions for leaf in _teardown_leaves(sub)]

    return [error]


def test_teardown_runs_even_when_startup_fails_after_pywire():
    """A nested lifespan that warms beans up and then explodes: the app
    never serves a request, but the beans it did build are still closed."""
    log: list[str] = []

    class WarmedPool:
        @pre_destroy
        def shutdown(self) -> None:
            log.append("warmed-pool")

    container = Container()
    container.register(WarmedPool)

    @asynccontextmanager
    async def failing_startup(app: FastAPI) -> AsyncIterator[None]:
        async with pywire_lifespan(container=container)(app):
            container.resolve(WarmedPool)
            raise RuntimeError("startup boom")
            yield  # unreachable; keeps this function an async generator

    app = FastAPI(lifespan=failing_startup)

    with pytest.raises(RuntimeError, match="startup boom"):
        with TestClient(app):
            pass

    assert log == ["warmed-pool"]


def test_a_failing_teardown_propagates_out_of_shutdown():
    """close() aggregates teardown failures into an ExceptionGroup; the
    lifespan lets it out rather than logging and swallowing it."""

    class BrokenPool:
        @pre_destroy
        def shutdown(self) -> None:
            raise ValueError("teardown boom")

    container = Container()
    container.register(BrokenPool)

    app = FastAPI(lifespan=pywire_lifespan(container=container))

    @app.get("/ping")
    def ping(pool: Autowired[BrokenPool]) -> dict:
        return {"ok": True}

    with pytest.raises(BaseException) as caught:
        with TestClient(app) as client:
            client.get("/ping")

    leaves = _teardown_leaves(caught.value)

    assert any(
        isinstance(leaf, ValueError) and str(leaf) == "teardown boom"
        for leaf in leaves
    )


def test_a_second_startup_rebuilds_and_tears_down_again():
    """close() leaves no 'closed' state: running the app's lifespan twice
    builds a fresh bean the second time and closes it too."""
    built: list[int] = []
    closed: list[int] = []

    class CycledPool:
        def __init__(self) -> None:
            self.serial = len(built)
            built.append(self.serial)

        @pre_destroy
        def shutdown(self) -> None:
            closed.append(self.serial)

    container = Container()
    container.register(CycledPool)

    app = FastAPI(lifespan=pywire_lifespan(container=container))

    @app.get("/ping")
    def ping(pool: Autowired[CycledPool]) -> dict:
        return {"serial": pool.serial}

    with TestClient(app) as client:
        assert client.get("/ping").json() == {"serial": 0}

    with TestClient(app) as client:
        assert client.get("/ping").json() == {"serial": 1}

    assert closed == [0, 1]
    assert app.state.pywire_container is container
