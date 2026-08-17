from fastapi import FastAPI
from fastapi.testclient import TestClient

from pywire import Autowired, Container
from pywire.fastapi import wire


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
