# PyWire — Dependency Injection Container

A minimal Dependency Injection container for Python 3.12+, inspired by Spring's `@Component` and `@Autowired`.

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
- Circular dependency detection and handling
- Explicit, independent containers
- `BeanDefinition` metadata for registered components
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

Field injection (`Autowired[T]` as a class attribute) and constructor injection can be used
together on the same class. An explicit keyword argument passed at construction time always
wins over auto-resolution. Note that manually constructing a registered component (e.g.
`SomeComponent(dep=manual_dep)`, bypassing `container.resolve(...)`) still registers that
instance as the container's singleton for all future `resolve()` calls — the patched
`__new__` always writes the instance into the registry, regardless of how it was created.

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
to run before the first *request* comes in — not before any route is decorated.

## Architecture

```
pywire/
├── container.py       # Main DI container
├── definitions.py     # BeanDefinition metadata
├── decorators.py      # @component decorator
├── exceptions.py      # Exception hierarchy
├── markers.py         # Autowired[T] marker
├── fastapi.py         # Optional FastAPI integration (wire())
└── __init__.py        # Public API
```

## Testing

```bash
uv run pytest
```
