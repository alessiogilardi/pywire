# FastAPI lifespan integration — Design

**Status:** approved, not implemented
**Date:** 2026-09-01
**Supersedes nothing.** Extends `src/pywire/fastapi.py`, adds nothing to `container.py`.

## Problem

`Container.close()` exists (0.6.0) and tears every resolved bean down in reverse
dependency order. Nothing in the FastAPI integration ever calls it. A service that
declares `@pre_destroy` on a connection pool and runs under uvicorn therefore never
tears that pool down — the one deployment shape the teardown feature exists to serve
is the one it does not reach.

`wire(app, container=...)` binds a container to an app by writing
`app.state.pywire_container`, and stops there: it has no view of the app's lifetime.

## Solution

A FastAPI lifespan **is** an async context manager that receives `app`. Both halves of
what is needed — writing `app.state.pywire_container` at startup, calling
`container.close()` at shutdown — fit inside it. So the lifespan becomes the single
entry point and `wire()` is no longer needed in user code.

```python
from fastapi import FastAPI

from pywire import Container
from pywire.fastapi import pywire_lifespan

container = Container()
container.register(ConnectionPool)
container.register(UserService)

app = FastAPI(lifespan=pywire_lifespan(container=container))
app.include_router(router)
```

Bare form, against the module-level default container (the one `@component` writes into):

```python
app = FastAPI(lifespan=pywire_lifespan)
```

Binding without teardown:

```python
app = FastAPI(lifespan=pywire_lifespan(close_on_shutdown=False))
```

Composed with the application's own lifespan — pywire outermost, so the user's shutdown
code still sees live beans:

```python
@asynccontextmanager
async def app_lifespan(app: FastAPI):
    async with pywire_lifespan(container=container)(app):
        await run_migrations()
        yield
        await flush_metrics()      # beans still alive here
    # container.close() runs here
```

## Public API

```python
@overload
def pywire_lifespan(app: FastAPI) -> AbstractAsyncContextManager[None]: ...
@overload
def pywire_lifespan(
    *,
    container: Container | None = None,
    close_on_shutdown: bool = True,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]: ...
```

Dual-form, dispatched on the positional argument, exactly like `component` in
`decorators.py`: a `FastAPI` positional means "I am already the lifespan"; no positional
means "I am a factory, call me and pass the result to `FastAPI(lifespan=...)`".

Startup, in order:

1. `resolved = container or get_default_container()`
2. if `app.state.pywire_container` is already set to a **different** object →
   `RuntimeError`
3. `app.state.pywire_container = resolved`

Shutdown, in a `finally`:

4. if `close_on_shutdown` → `await asyncio.to_thread(resolved.close)`

## Decisions

Each of these was put to the user and confirmed; the reasoning is recorded so the
implementation does not relitigate it.

| # | Decision | Why |
|---|---|---|
| 1 | Name is `pywire_lifespan`, not `lifespan` | `lifespan` is the name almost every application gives its *own* function; `from pywire.fastapi import lifespan` followed by `async def lifespan(app)` shadows it. Consistent with `app.state.pywire_container`. |
| 2 | `wire()` stays, deprecated in documentation only | It works, it is public, and 0.x has no removal date to offer in exchange for a runtime `DeprecationWarning` in other people's logs. Revisit once `pywire_lifespan` has displaced real usage. |
| 3 | Teardown is on by default in **both** forms; `close_on_shutdown=False` is the escape hatch | One rule regardless of where the container came from. The bare form closing the process-global default container is a real hazard for a test suite with two apps; it is documented, and `close_on_shutdown=False` covers it, rather than making the two forms behave differently. |
| 4 | `try: yield / finally: close()` | Written as a plain `yield` followed by `close()`, an exception raised during a nested startup or while serving is thrown into the generator at the `yield` and the teardown never runs — losing teardown exactly when the app is dying is the worst case for this feature. |
| 5 | `asyncio.to_thread`, not `anyio.to_thread.run_sync` | `close()` is synchronous and may block on I/O; running it on the event loop stalls the rest of shutdown. `anyio` is not declared anywhere in `pyproject.toml` — it arrives only transitively through starlette — and `dependencies = []` is an identity trait of this library. `asyncio.to_thread` is stdlib and does the same job. Trio is not a real deployment target for FastAPI. |
| 6 | Container conflict raises `RuntimeError`; identical object passes | If `wire(app, container=A)` and `pywire_lifespan(container=B)` are both used, one configuration is dead and its beans are never closed. Same posture as `resolve_teardown` rejecting `on_close` + `@pre_destroy` instead of inventing a precedence rule. Identity comparison (`is not`), not equality. |
| 7 | `RuntimeError`, not a new `PyWireError` subclass | No bean is involved; `chain` and `requester` — the only things `PyWireError` carries — would be empty. `wire()` already raises a builtin `TypeError` for a bad target, so integration-level misuse using standard exceptions is the established precedent. |
| 8 | `TypeError` for a non-`FastAPI` positional | `pywire_lifespan(container)` written without the keyword would otherwise fail as an `AttributeError` deep inside. Reuses `wire()`'s existing guard and message shape (`fastapi.py:169-171`). |
| 9 | `TypeError` for `pywire_lifespan(app, container=...)` | Not reachable through either overload. Running it while ignoring `container` would silently bind the wrong container — the same failure `component(cls, as_type=...)` refuses. |
| 10 | `pywire_lifespan()` with empty parentheses is legal | Unlike `component()`, nothing mandatory is missing: both parameters are optional and the call has an obvious meaning. `component()` raises because `as_type` is required there, not for symmetry's own sake. |
| 11 | `app.state.pywire_container` is left set after shutdown | `close()` is explicitly documented as having no "closed" state — a later `resolve()` just rebuilds. Clearing the binding would introduce one, and would fail by silently falling back to a *different* container rather than by raising. |
| 12 | Teardown failures propagate | `close()` raises an `ExceptionGroup` rather than logging and swallowing; the lifespan is not the place to reverse that. Under `try/finally` with an exception already in flight, Python records the original in `__context__`. |

## Non-goals

- Eager resolution of registered beans at startup (fail-fast). A different feature that
  happens to fit the same API; pywire is deliberately lazy everywhere.
- WebSocket routes. `add_api_websocket_route` remains unpatched, as before.
- `atexit` hooks.
- Any change to `container.py`. `close()` already does everything needed.
