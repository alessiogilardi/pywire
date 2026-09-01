# FastAPI lifespan integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `pywire.fastapi` a `pywire_lifespan` entry point that binds a `Container`
to a `FastAPI` app at startup and calls `Container.close()` at shutdown, so a bean's
`@pre_destroy` / `on_close` teardown actually runs when the service stops.

**Architecture:** A FastAPI lifespan is an async context manager that receives `app`, so
it can do both halves of the job: write `app.state.pywire_container` at startup (what
`wire()` does today) and `await asyncio.to_thread(container.close)` at shutdown. Dual-form
callable — `FastAPI(lifespan=pywire_lifespan)` bare, or
`FastAPI(lifespan=pywire_lifespan(container=..., close_on_shutdown=...))` — dispatched on
the positional argument exactly like `component` in `decorators.py`. `wire()` is untouched
functionally and marked deprecated in prose only. `container.py` is not modified at all.

**Tech Stack:** Python 3.13, `uv`, pytest, ruff, pyright, FastAPI (optional extra, already
in the dev group). `asyncio.to_thread` and `contextlib.asynccontextmanager` are stdlib —
no new dependency, declared or transitive.

**Spec:** `docs/superpowers/specs/2026-09-01-fastapi-lifespan-design.md` — brainstormed and
grilled (13 questions) before this plan. Read it first; this plan does not re-explain *why*
each decision was made, only *how* to build it.

## Global Constraints

- Python **3.13**; PEP 695 generics where generics are needed, matching existing code.
- `uv` only. `uv run pytest`, `uv run ruff check .`, `uv run pyright`. Never `pip`.
- All docstrings, comments, and error messages in **English**.
- ruff: `E, F, I, UP, RUF, ANN401`, target `py313`, line length **88**, first-party group
  `pywire`.
- **Every commit must leave the repo green**: `uv run pytest`, `uv run ruff check .`,
  `uv run pyright` all pass at every one of this plan's commits.
- **No version bump.** `pyproject.toml` stays at `0.6.0`. No new entry in
  `[project.optional-dependencies]` — in particular **do not add `anyio`**.
- **No changes to `src/pywire/container.py`.** `close()` already provides everything.
- **No changes to the 20 existing tests** in `tests/test_fastapi_integration.py`. They do
  not exercise a lifespan and have no reason to start; every new test in this plan uses
  `with TestClient(app) as client:` because **the lifespan only runs inside that `with`**.
- `pywire_lifespan` is exported from `pywire.fastapi` only. `pywire/__init__.py` must not
  import it — that module has no runtime dependency on FastAPI and must keep none.
- Tests assert through the public API and through side-effecting callables (lists appended
  to), never by reading `container._registry` or other private state — except where an
  existing test in the file already does so for a documented reason.
- A bean is only torn down if it was actually **resolved**. Every teardown test must issue
  a request (or an explicit `resolve()`) inside the `with` block, or it asserts nothing.

---

### Task 1: `pywire_lifespan` — dual form, binding, teardown

The happy path only. Guard rails are Task 2 and `try/finally` is Task 3, each driven by
its own failing test — do not write them here.

**Files:**
- Modify: `src/pywire/fastapi.py` (add imports at the top; append the new public function
  and its private helper after `wire()`)
- Test: `tests/test_fastapi_integration.py` (append; do not touch existing tests)

**Interfaces:**
- Consumes: `pywire.decorators.get_default_container` (already imported in the module),
  `pywire.container.Container` (already imported under `TYPE_CHECKING`).
- Produces:
  - `pywire_lifespan(app: FastAPI) -> AbstractAsyncContextManager[None]`
  - `pywire_lifespan(*, container: Container | None = None, close_on_shutdown: bool = True)
    -> Callable[[FastAPI], AbstractAsyncContextManager[None]]`
  - Private `_run(app, container, close_on_shutdown)` async context manager. Tasks 2 and 3
    both extend `_run`'s body and `pywire_lifespan`'s dispatch; no new names.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_fastapi_integration.py`. Note the component classes are defined
*inside* each test function: this test module has no `from __future__ import annotations`,
so `Autowired[LocalClass]` in an endpoint signature is a real object rather than a string
and resolves without needing module-level scope. (If `get_type_hints` ever complains, move
the class to module scope — do not add `from __future__ import annotations` to this file,
it would change how every existing test's annotations are evaluated.)

```python
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
```

Extend the import block at the top of the file (do not reorder the existing lines beyond
what `ruff --fix` does for `I`):

```python
from pywire import (
    AnnotationResolutionError,
    Autowired,
    Container,
    component,
    pre_destroy,
)
from pywire.fastapi import pywire_lifespan, wire
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_fastapi_integration.py -k lifespan_or_shutdown -v`
(or simply the whole file).
Expected: collection error — `ImportError: cannot import name 'pywire_lifespan' from
'pywire.fastapi'`.

- [ ] **Step 3: Write the minimal implementation**

Add to the import block at the top of `src/pywire/fastapi.py`:

```python
import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import TYPE_CHECKING, Any, cast, overload
```

Append after `wire()`:

```python
@overload
def pywire_lifespan(app: FastAPI) -> AbstractAsyncContextManager[None]: ...


@overload
def pywire_lifespan(
    *,
    container: Container | None = None,
    close_on_shutdown: bool = True,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]: ...


def pywire_lifespan(
    app: FastAPI | None = None,
    *,
    container: Container | None = None,
    close_on_shutdown: bool = True,
) -> (
    AbstractAsyncContextManager[None]
    | Callable[[FastAPI], AbstractAsyncContextManager[None]]
):
    """ASGI lifespan that binds a container to an app and closes it on shutdown.

    Usable bare or called with configuration, the same dual form @component
    uses:

        app = FastAPI(lifespan=pywire_lifespan)
        app = FastAPI(lifespan=pywire_lifespan(container=container))
        app = FastAPI(lifespan=pywire_lifespan(close_on_shutdown=False))

    Startup writes app.state.pywire_container -- everything wire() does --
    so an app using this needs no wire() call. Shutdown calls
    Container.close(), which is what makes a bean's @pre_destroy or
    on_close hook actually run when the service stops.

    Bare, it binds and closes the module-level default container: the same
    one @component registers into, and process-global. In a test suite where
    two apps share it, one app's shutdown tears down beans the other still
    holds -- pass close_on_shutdown=False for the apps that should not own
    that lifetime.
    """
    if app is not None:
        return _run(app, None, True)

    def build(target: FastAPI) -> AbstractAsyncContextManager[None]:
        return _run(target, container, close_on_shutdown)

    return build


@asynccontextmanager
async def _run(
    app: FastAPI,
    container: Container | None,
    close_on_shutdown: bool,
) -> AsyncIterator[None]:
    """Bind at startup, tear down at shutdown.

    close() is synchronous and a teardown hook may block on real I/O
    (draining a pool, joining a thread), so it runs in a worker thread
    rather than on the event loop, which still has the rest of the
    application's shutdown to run. asyncio.to_thread, not anyio: this
    library declares no dependencies and is not going to start with one it
    only gets transitively.
    """
    resolved = container or get_default_container()
    app.state.pywire_container = resolved

    yield

    if close_on_shutdown:
        await asyncio.to_thread(resolved.close)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_fastapi_integration.py -v`
Expected: PASS, including all 20 pre-existing tests.

- [ ] **Step 5: Verify the type checker accepts both forms**

Run: `uv run pyright`
Expected: 0 errors. The two `@overload`s are what let `FastAPI(lifespan=pywire_lifespan)`
and `FastAPI(lifespan=pywire_lifespan(container=c))` both satisfy FastAPI's `Lifespan`
parameter type. If pyright rejects the bare form, the fix is in the *first* overload's
return type (it must be `AbstractAsyncContextManager[None]`, not `AsyncIterator[None]`) —
not a `cast` at the call site in the tests.

- [ ] **Step 6: Lint**

Run: `uv run ruff check .`
Expected: no findings. `ANN401` does not fire here — no bare `Any` is introduced.

- [ ] **Step 7: Commit**

```bash
git add src/pywire/fastapi.py tests/test_fastapi_integration.py
git commit -m "✨ Add pywire_lifespan: bind a container and close it on app shutdown"
```

---

### Task 2: Guard rails — wrong positional, app + config, container conflict

Three refusals, all of them cases that would otherwise fail silently or with an error that
names the wrong thing.

**Files:**
- Modify: `src/pywire/fastapi.py` (`pywire_lifespan`'s dispatch branch and `_run`'s startup
  section, both from Task 1)
- Test: `tests/test_fastapi_integration.py`

**Interfaces:**
- Consumes: `pywire_lifespan` and `_run` as built in Task 1.
- Produces: no new names. `pywire_lifespan` gains a `TypeError` on a non-`FastAPI`
  positional and on `app` + config together; `_run` gains a `RuntimeError` at startup when
  `app.state.pywire_container` already holds a different container.

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_fastapi_integration.py -v`
Expected: the first three FAIL — respectively with an `AttributeError` about
`Container.state` (no `TypeError` raised), no exception at all, and no exception at all.
`test_the_same_container_configured_twice_is_accepted` already passes; keep it, it is the
regression guard for the guard.

- [ ] **Step 3: Write the implementation**

In `pywire_lifespan`, replace the dispatch branch from Task 1:

```python
    if app is not None:
        if container is not None or not close_on_shutdown:
            # Neither @overload reaches this. Binding app while dropping the
            # configuration would silently use the wrong container -- the one
            # silent failure component() refuses for the same reason.
            raise TypeError(
                "pywire_lifespan() cannot take both an app and configuration. "
                "Write FastAPI(lifespan=pywire_lifespan(container=...)) instead."
            )

        if not isinstance(app, FastAPI):
            got = type(app).__name__
            raise TypeError(
                f"pywire_lifespan() requires a FastAPI instance, got {got}"
            )

        return _run(app, None, True)
```

In `_run`, replace the startup section:

```python
    resolved = container or get_default_container()
    existing = getattr(app.state, "pywire_container", None)

    if existing is not None and existing is not resolved:
        raise RuntimeError(
            "This app is already bound to a different pywire container "
            "(app.state.pywire_container). Configure it once -- either with "
            "pywire_lifespan(container=...) or with wire(app, container=...), "
            "not both."
        )

    app.state.pywire_container = resolved
```

Note the `close_on_shutdown` half of the first guard: `pywire_lifespan(app,
close_on_shutdown=False)` is just as unreachable through the overloads as the `container`
case, and just as silently wrong — it would tear down a container the caller asked to
leave alone.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_fastapi_integration.py -v`
Expected: PASS, all tests in the file.

- [ ] **Step 5: Lint and type-check**

Run: `uv run ruff check . && uv run pyright`
Expected: no findings, 0 errors.

- [ ] **Step 6: Commit**

```bash
git add src/pywire/fastapi.py tests/test_fastapi_integration.py
git commit -m "✅ Reject a bad pywire_lifespan call and a doubly-bound app"
```

---

### Task 3: Teardown survives a failing startup, and failures propagate

Task 1 wrote `yield` followed by `close()`. That loses the teardown in exactly the case it
matters most. These tests are what force the `try/finally`.

**Files:**
- Modify: `src/pywire/fastapi.py` (`_run`'s body)
- Test: `tests/test_fastapi_integration.py`

**Interfaces:**
- Consumes: `_run` as built in Tasks 1-2.
- Produces: no new names; `_run`'s `yield` moves inside a `try`, the teardown into
  `finally`. The module-level `_teardown_leaves` helper below is test-only.

- [ ] **Step 1: Write the failing tests**

Add the helper at module scope in the test file, above the new tests:

```python
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
```

Then the tests:

```python
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

    with pytest.raises(BaseException) as caught:  # noqa: PT011, B017
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
```

Add to the test file's imports:

```python
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_fastapi_integration.py -v`
Expected: `test_teardown_runs_even_when_startup_fails_after_pywire` FAILS on
`assert log == ["warmed-pool"]` (the list is empty — the code after `yield` never ran).
The other two already pass; they are the regressions guards for behaviour Task 1 gave for
free, and they must keep passing after Step 3.

- [ ] **Step 3: Write the implementation**

Replace the tail of `_run`:

```python
    try:
        yield
    finally:
        # An exception raised during a nested startup, or while serving, is
        # thrown in at the yield -- without the finally, that is precisely
        # when the teardown would be skipped. If close() then raises too,
        # Python keeps the original in __context__.
        if close_on_shutdown:
            await asyncio.to_thread(resolved.close)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_fastapi_integration.py -v`
Expected: PASS, all tests in the file.

- [ ] **Step 5: Run the whole suite, lint and type-check**

Run: `uv run pytest && uv run ruff check . && uv run pyright`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add src/pywire/fastapi.py tests/test_fastapi_integration.py
git commit -m "🐛 Run pywire_lifespan teardown even when startup fails"
```

---

### Task 4: Documentation

No code. `wire()` keeps working and is deprecated in prose only — no runtime
`DeprecationWarning` (spec decision 2).

**Files:**
- Modify: `src/pywire/fastapi.py` (`wire()`'s docstring only)
- Modify: `README.md:284-371` (the `## FastAPI Integration` section and the architecture
  tree)
- Modify: `CLAUDE.md` (the `### FastAPI integration` section and the Module layout table
  row for `fastapi.py`)

**Interfaces:**
- Consumes: the final `pywire_lifespan` signature and semantics from Tasks 1-3.
- Produces: nothing importable.

- [ ] **Step 1: Add the deprecation note to `wire()`'s docstring**

Insert as the second paragraph of `wire()`'s docstring in `src/pywire/fastapi.py`, keeping
the rest unchanged:

```
    Deprecated in favour of pywire_lifespan, which does this binding at
    startup *and* closes the container at shutdown, so a bean's teardown
    actually runs when the service stops. wire() still works and is not
    scheduled for removal; use it when an app deliberately does not own its
    container's lifetime -- pywire_lifespan(close_on_shutdown=False) covers
    that case too.
```

- [ ] **Step 2: Rewrite `README.md`'s FastAPI usage section**

Two edits in `## FastAPI Integration` (`README.md:284-355`):

**(a)** In `### Usage`, change the two lines of the existing example so the lifespan is the
entry point — everything else in that example stays as it is:

```python
from pywire.fastapi import pywire_lifespan     # was: from pywire.fastapi import wire

app = FastAPI(lifespan=pywire_lifespan(container=container))   # was: app = FastAPI()
                                                               #      wire(app, container=container)
```

and change the sentence at `README.md:298` from "Call `wire()` once on your `FastAPI` app,
at any point relative to route/router decoration:" to "Pass `pywire_lifespan` to your
`FastAPI` app; routes can be decorated at any point relative to it:". The sentence at
`README.md:328` about the fallback to the default container stays true verbatim for
`pywire_lifespan` — change only the function name in it.

**(b)** Insert this new subsection immediately after `### Usage`, before `### Resolution`:

````markdown
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
still sees live beans:

```python
@asynccontextmanager
async def app_lifespan(app: FastAPI):
    async with pywire_lifespan(container=container)(app):
        await run_migrations()
        yield
        await flush_metrics()   # beans still alive here
    # container.close() runs here
```

`close()` runs in a worker thread, so a teardown that blocks on I/O does not stall the
rest of the application's shutdown. Teardown failures propagate as the same
`ExceptionGroup` `Container.close()` raises — they are never swallowed. Configuring one
app with both `wire(app, container=A)` and `pywire_lifespan(container=B)` raises a
`RuntimeError` at startup: one of the two would be dead configuration whose beans are
never closed. Naming the same container twice is fine.

`wire()` remains available and unchanged, but `pywire_lifespan` supersedes it: `wire()`
only binds, and an app wired that way never tears its beans down.
````

Also update the architecture tree line:

```
├── fastapi.py         # Optional FastAPI integration (pywire_lifespan(), wire())
```

And add a bullet to `## Features` (`README.md:23-38`) alongside the existing teardown one:

```markdown
- **FastAPI lifespan** — `FastAPI(lifespan=pywire_lifespan(container=container))` binds the
  container and tears every bean down when the service stops.
```

- [ ] **Step 3: Update `CLAUDE.md`**

In `### FastAPI integration`, add a bullet after the `wire()` bullet:

```markdown
- `pywire_lifespan(app=None, *, container=None, close_on_shutdown=True)` is the entry
  point that supersedes `wire()`: as an ASGI lifespan it does `wire()`'s binding at
  startup and `Container.close()` at shutdown, which is the only thing in the FastAPI
  integration that ever fires a bean's `@pre_destroy` / `on_close`. Dual-form on the
  positional argument, exactly like `component` in `decorators.py` — a `FastAPI`
  positional means "I am the lifespan", no positional means "I am a factory" — with a
  `TypeError` for the combination no `@overload` reaches (`app` plus configuration) and
  for a positional that is not a `FastAPI`, mirroring `wire()`'s own guard.
  `close()` runs through `asyncio.to_thread`: it is synchronous and may block on I/O,
  and `anyio` is deliberately not used because it is not a declared dependency of this
  library — it only arrives transitively through starlette. The teardown sits in a
  `finally`, so a nested startup that fails after pywire's own still closes whatever was
  built. Startup raises `RuntimeError` — not a `PyWireError`: no bean is involved and
  `chain`/`requester` would be empty — if `app.state.pywire_container` already holds a
  *different* container, since one of the two configurations would be dead and its beans
  never closed; the same object twice is accepted. The binding is left in place after
  shutdown, because `close()` has no "closed" state and clearing it would silently fall
  back to a different container. `wire()` is unchanged and deprecated in documentation
  only — no runtime `DeprecationWarning` while 0.x has no removal date to offer.
```

And the Module layout table row:

```markdown
| `fastapi.py` | Optional FastAPI integration: `pywire_lifespan()`, `wire()`, `_install_patch`, `_wire_endpoint`, `_resolve_autowired` — resolves bare `Autowired[T]` route parameters via a global `add_api_route` patch, and ties container teardown to the ASGI lifespan |
```

- [ ] **Step 4: Verify the documented examples are real**

Run: `uv run pytest && uv run ruff check . && uv run pyright`
Expected: all green. Then re-read the README snippets against
`tests/test_fastapi_integration.py` — every construct shown (`pywire_lifespan(container=)`,
the bare form, `close_on_shutdown=False`, the nested `async with`) must have a
corresponding passing test from Tasks 1-3. Fix the prose, not the tests, if they disagree.

- [ ] **Step 5: Commit**

```bash
git add README.md CLAUDE.md src/pywire/fastapi.py
git commit -m "📝 Document pywire_lifespan and deprecate wire() in prose"
```

---

## Out of scope

Deliberately excluded (spec, Non-goals) — do not add them opportunistically:

- Eager resolution of beans at startup (fail-fast).
- WebSocket routes (`add_api_websocket_route` stays unpatched).
- `atexit` hooks on the default container.
- Any modification to `src/pywire/container.py`.
- A version bump. `pyproject.toml` stays at `0.6.0`; releasing is a separate, explicit
  request that runs `./scripts/bump-version.sh`.
