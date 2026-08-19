import functools
import importlib

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pydantic_settings import BaseSettings, SettingsConfigDict

import pywire.fastapi
from pywire import AnnotationResolutionError, Autowired, Container, component
from pywire.fastapi import wire


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
