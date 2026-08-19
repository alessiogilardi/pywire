# PyWire — Dependency Injection Container

A minimal Dependency Injection container for Python 3.13+, inspired by Spring's `@Component` and `@Autowired`.

## Overview

PyWire provides a lightweight API for dependency injection:

```python
from pywire import Autowired, component


@component
class Repository:
    client: Autowired["DBClient"]


@component
class DBClient:
    pass
```

## Features

- Component registration via `@component` decorator
- Lazy singleton creation
- Field injection via `Autowired[T]`
- Constructor injection via `Autowired[T]` parameters in `__init__`
- Forward references support
- Circular dependencies through fields; a cycle passing through a
  constructor parameter is rejected with the dependency chain
- Explicit, independent containers — the same class registered in two
  containers yields two independent singletons
- Typed exception hierarchy rooted at PyWireError
- Optional FastAPI integration (`pywire.fastapi.wire`) for bare `Autowired[T]` route parameters

## Installation

```bash
uv pip install -e .
```

## Usage

### Register Components

```python
from pywire import component, Container

@component
class UserRepository:
    pass

@component
class UserService:
    repository: Autowired["UserRepository"]
```

### Create and Use a Container

```python
container = Container()
container.register(UserRepository)
container.register(UserService)

service = container.resolve(UserService)
```

### Constructor Injection

`Autowired[T]` also works as a constructor parameter annotation — the container resolves and
injects the dependency before your own `__init__` body runs:

```python
@component
class UserService:
    def __init__(self, repository: Autowired[UserRepository]) -> None:
        self.repository = repository
```

Field injection and constructor injection can be used together on the same class, and
an injected field is set **before** your `__init__` body runs, so `__init__` can read
it. An explicit keyword argument passed to `container.resolve(...)` is not supported;
construct such objects yourself and register the instance.

## Components are wired by the container, not by the class

`register()` never modifies the class it registers. A component is wired only when the
container builds it:

```python
container.register(Repository)

wired = container.resolve(Repository)   # Autowired fields injected
unwired = Repository()                  # plain Python: no injection
```

`Repository()` is an ordinary object — its `Autowired` fields are absent, and reading
one raises `AttributeError`. Always go through `container.resolve(...)` (or
`container.get(...)`).

`container.clear_instances()` drops every cached singleton while keeping every
registration, for callers that want a fresh object graph.

### Inherited fields

An `Autowired` field declared on a base class is injected into every subclass the
container builds. Re-annotating it without `Autowired` opts out:

```python
class Base:
    repo: Autowired[Repo]

class Child(Base):
    repo: Repo          # not injected; Child supplies it some other way
```

### Cycles

Two components may depend on each other through **fields**. A cycle that passes
through a **constructor parameter** is rejected with `CircularDependencyError` and the
chain, whichever end you resolve from — a constructor argument has to exist before
`__init__` can be called, so such a cycle has no fixed point. Convert one dependency
from a constructor parameter to a field to express an intentional cycle.

One caveat: while a field cycle is being closed, the partner's `__init__` observes a
partially-wired object. Storing the reference is fine; calling methods on it from
inside `__init__` is not.

### Errors

Everything pywire raises derives from `PyWireError`:

| Error | Meaning |
|---|---|
| `RegistrationError` | The same class was registered twice in one container |
| `UnconstructibleComponentError` | The container can never build this class — `__new__` needs arguments, a parameter cannot be supplied, or an injected field cannot be set |
| `AnnotationResolutionError` | An `Autowired[...]` annotation names a type that cannot be resolved |
| `DependencyResolutionError` | A dependency is not registered, or failed to build |
| `CircularDependencyError` | A dependency cycle passes through a constructor parameter (a `DependencyResolutionError`) |

Messages carry the resolution chain and the member that asked for the dependency, so a
four-deep failure reads as one sentence.

## FastAPI Integration

PyWire ships an optional FastAPI integration that lets route handlers declare their
dependencies as bare `Autowired[T]` parameters, resolved automatically on every request —
no manual `Depends(...)` wiring required.

### Installation

```bash
uv pip install -e ".[fastapi]"
```

### Usage

Call `wire()` once on your `FastAPI` app, at any point relative to route/router decoration:

```python
from fastapi import FastAPI

from pywire import Autowired, Container
from pywire.fastapi import wire


class UserRepository:
    pass


class UserService:
    repository: Autowired[UserRepository]


container = Container()
container.register(UserRepository)
container.register(UserService)

app = FastAPI()
wire(app, container=container)


@app.get("/users")
def list_users(service: Autowired[UserService]) -> dict:
    return {"repository": type(service.repository).__name__}
```

If `container` is omitted, `wire()` falls back to the same module-level default container
used by `@component`.

### Resolution

Decorating a route with a bare `Autowired[T]` parameter is always safe, on any `APIRouter`,
regardless of whether `wire(app, ...)` has run yet — this holds even for the common pattern of
one `APIRouter` per module, decorated at import time, later mounted onto the app with
`app.include_router(router)` inside a `create_app()` factory. The actual container lookup is
deferred to request time: it reads `app.state.pywire_container` (set by `wire()`), falling back
to the default container if `wire()` was never called for that app. `wire(app, ...)` only needs
to run before the first *request* comes in — not before any route is decorated. (HTTP routes
only — WebSocket routes are not covered, same as before this redesign.)

This safety does require `pywire.fastapi` itself to be imported before any module that
decorates a route with `Autowired[T]` — e.g. `from pywire.fastapi import wire` near the top of
your app's entrypoint, before importing your router modules. The global patch that makes
decoration order-independent is installed at `pywire.fastapi`'s own import time; if a router
module is imported first, without `pywire.fastapi` anywhere in `sys.modules` yet, the same
decoration-time error this redesign eliminates can still occur.

If you forget to call `wire(app, container=...)` for a specific app, this fails silently rather
than loudly — parameters resolve against the default container, which may not have the
component you expect registered (or may hold a different instance than intended). Always call
`wire(app, container=...)` explicitly in apps that use more than the default container.

If you previously called `wire(router, ...)` on an `APIRouter`, remove that call — `wire()` now
only accepts the `FastAPI` app; routers no longer need to be wired individually.

## Architecture

```
pywire/
├── container.py       # Registry and resolve-time construction
├── plans.py           # InjectionPlan: what a class needs
├── definitions.py     # BeanDefinition metadata
├── decorators.py      # @component decorator
├── exceptions.py      # Exception hierarchy
├── markers.py         # Autowired[T] marker and annotation evaluation
├── fastapi.py         # Optional FastAPI integration (wire())
└── __init__.py        # Public API
```

## Testing

```bash
uv run pytest
```
