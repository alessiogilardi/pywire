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
- **FastAPI lifespan** — `FastAPI(lifespan=pywire_lifespan(container=container))` binds the
  container and tears every bean down when the service stops.
- Lifecycle teardown via `@pre_destroy` or `on_close=`, run by `Container.close()` in
  reverse build order

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
| `RegistrationError` | A key is already registered, `None` was pushed, or a factory is a coroutine function |
| `UnconstructibleComponentError` | The container can never build this class — `__new__` needs arguments, a parameter cannot be supplied, or an injected field cannot be set |
| `AnnotationResolutionError` | An `Autowired[...]` annotation names a type that cannot be resolved |
| `DependencyResolutionError` | A dependency is not registered, or failed to build |
| `CircularDependencyError` | A dependency cycle passes through a constructor parameter (a `DependencyResolutionError`) |

Messages carry the resolution chain and the member that asked for the dependency, so a
four-deep failure reads as one sentence.

## Objects the container cannot build

`@component` covers classes the container constructs. Configuration values,
database engines and HTTP clients are built elsewhere — by you, at the entry
point — so they are *pushed* into the container instead.

```python
# main.py -- the composition root, the only place the container is named
config = AppConfig(_yaml_file=args.config)     # fails fast, here
container = get_default_container()

container.register_instance(config)            # key: AppConfig
container.register_instance(config.postgres)   # key: PostgresConfig
container.register_factory(Engine, lambda: create_engine(config.postgres.dsn))
```

`register_instance` publishes an object you already have; the key is its runtime
type. `register_factory` publishes a recipe: the callable runs at most once, on
first resolution, so nothing is built unless something asks for it.

Two rules to keep in mind:

- **A pushed object is not wired.** The container injects only into instances it
  constructs. `register_instance(MyService())` returns an object whose `Autowired`
  fields were never set.
- **A factory runs under the container's lock**, like every `__init__`. A factory
  that blocks on another thread's `resolve()` deadlocks, and one that does I/O
  serializes every resolution in that container while it runs.

### Depending on an abstraction

`as_type=` registers a bean under a supertype or `Protocol` instead of its own
class, so consumers never name the implementation:

```python
@repository(as_type=UserRepository)
class PostgresUserRepo:
    db: Autowired[PostgresConfig]

@service
class UserService:
    repo: Autowired[UserRepository]     # does not know PostgresUserRepo exists
```

`as_type` **rebinds**: after it, `resolve(PostgresUserRepo)` fails. To have both
keys, register twice — and know you are creating two beans.

### When you do not need push

A settings class that is fully determined by the environment builds itself, so it
needs no push at all:

```python
class PostgresConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="POSTGRES_")
    host: str

container.register(PostgresConfig)     # resolve() calls PostgresConfig()
```

Push is for configuration that depends on entry-point input — a CLI argument, a
file chosen at runtime, a value already in memory.

### One bean per type

The registry is keyed by type, with no qualifiers. Two sub-configurations of the
same type cannot both be registered:

```python
container.register_instance(config.primary_db)   # PostgresConfig
container.register_instance(config.replica_db)   # RegistrationError
```

Give them distinct types in the configuration model
(`class ReplicaDbConfig(PostgresConfig): ...`). The failure is immediate and loud —
at startup, never a wrong bean injected quietly.

## Tearing beans down

A bean that owns an external resource -- a connection pool, a file handle -- needs
to release it when the container's work is done. Two ways to declare how, matching
the two ways a bean gets built:

```python
from pywire import pre_destroy, service

@service
class Cache:
    @pre_destroy
    def shutdown(self) -> None:
        self._connection.close()
```

For a third-party class you cannot decorate, declare it at the registration call
instead:

```python
container.register_factory(
    Engine,
    lambda: create_engine(config.postgres.dsn),
    on_close=lambda engine: engine.dispose(),
)
```

The two are mutually exclusive for the same bean -- registering `on_close=` for a
class that already has a `@pre_destroy` method is a `RegistrationError`, not a
silent override.

`Container.close()` -- or the equivalent `with Container() as container:` -- tears
every resolved bean down in reverse of the order it was built, so a bean's
dependents are always closed before the bean itself. Every teardown is attempted
regardless of earlier failures; if any raised, `close()` raises them together as an
`ExceptionGroup` once every bean has been tried.

```python
with Container() as container:
    container.register_factory(Engine, lambda: create_engine(dsn), on_close=lambda e: e.dispose())
    container.register(GoldenSetLabelingService)

    container.resolve(GoldenSetLabelingService)(run_id)
# engine disposed here, deterministically, even if the block raised
```

A bean without `@pre_destroy` or `on_close` simply has no teardown -- there is no
fallback that guesses at a `close()`/`__exit__` method on your behalf. `close()`
leaves the container's registrations intact, so it is safe to call more than once,
and a `resolve()` afterward just rebuilds -- there is no "closed" state to trip
over. Nothing is torn down automatically: the default container (what `@component`
writes into) is closed explicitly, with `get_default_container().close()`, never at
process exit.

A bean registered with `register_instance()` is the one exception to "just
rebuilds": a pushed instance is stored as a factory that always returns the same
object, so after `close()` tears it down, `resolve()` hands back that identical,
already-torn-down object rather than a fresh one -- and closing again afterward
re-runs its teardown against that same, already-closed object.

## FastAPI Integration

PyWire ships an optional FastAPI integration that lets route handlers declare their
dependencies as bare `Autowired[T]` parameters, resolved automatically on every request —
no manual `Depends(...)` wiring required.

### Installation

```bash
uv pip install -e ".[fastapi]"
```

### Usage

Pass `pywire_lifespan` to your `FastAPI` app; routes can be decorated at any point relative to it:

```python
from fastapi import FastAPI

from pywire import Autowired, Container
from pywire.fastapi import pywire_lifespan


class UserRepository:
    pass


class UserService:
    repository: Autowired[UserRepository]


container = Container()
container.register(UserRepository)
container.register(UserService)

app = FastAPI(lifespan=pywire_lifespan(container=container))


@app.get("/users")
def list_users(service: Autowired[UserService]) -> dict:
    return {"repository": type(service.repository).__name__}
```

If `container` is omitted, `pywire_lifespan` falls back to the same module-level default container
used by `@component`.

### Lifespan and teardown

`pywire_lifespan` binds the container at startup and calls `Container.close()` at
shutdown, so every bean's `@pre_destroy` / `on_close` hook runs when the service stops:

```python
from fastapi import FastAPI

from pywire import Container
from pywire.fastapi import pywire_lifespan

container = Container()
container.register(ConnectionPool)
container.register(UserService)

app = FastAPI(lifespan=pywire_lifespan(container=container))
```

Bare, it binds and closes the module-level default container — the one `@component`
writes into:

```python
app = FastAPI(lifespan=pywire_lifespan)
```

That container is process-global, so in a test suite where two apps share it, one app's
shutdown tears down beans the other still holds. `pywire_lifespan(close_on_shutdown=False)`
binds without closing, for the apps that should not own that lifetime.

To compose it with your own lifespan, nest pywire's on the outside so your shutdown code
runs while beans are still alive. This works because `pywire_lifespan`'s teardown sits in a
`finally` block: it closes the container whether your nested startup succeeds or fails, and
only after your outer shutdown code completes.

```python
@asynccontextmanager
async def app_lifespan(app: FastAPI):
    async with pywire_lifespan(container=container)(app):
        await run_migrations()
        yield
        await flush_metrics()   # run before container.close()
    # container.close() runs after this block exits
```

`close()` runs in a worker thread, so a teardown that blocks on I/O does not stall the
rest of the application's shutdown. Teardown failures propagate as the same
`ExceptionGroup` `Container.close()` raises — they are never swallowed. Configuring one
app with both `wire(app, container=A)` and `pywire_lifespan(container=B)` raises a
`RuntimeError` at startup: one of the two would be dead configuration whose beans are
never closed. Naming the same container twice is fine.

`wire()` remains available and unchanged, but `pywire_lifespan` supersedes it: `wire()`
only binds, and an app wired that way never tears its beans down.

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
├── aliases.py         # service/repository/agent/client/provider — synonyms for @component
├── exceptions.py      # Exception hierarchy
├── markers.py         # Autowired[T] marker and annotation evaluation
├── lifecycle.py       # @pre_destroy marker and teardown resolution
├── fastapi.py         # Optional FastAPI integration (pywire_lifespan(), wire())
└── __init__.py         # Public API
```

## Testing

```bash
uv run pytest
```
