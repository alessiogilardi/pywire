# FastAPI integration: wire(app)-only redesign

Date: 2026-08-17
Status: Approved for implementation planning

## Motivation

`pywire.fastapi.wire()` currently works by swapping `route_class` on a specific
`FastAPI`/`APIRouter` target. This only affects routes decorated *after* that
specific object was wired. In the common real-world pattern — one `APIRouter`
per module, decorated via `@router.get(...)` at import time, later mounted onto
the app via `app.include_router(router)` inside a `create_app()` factory —
`wire(app)` alone never reaches those routes: `wire()` only ever touched
`app.router`, never the independently-constructed `APIRouter()` instances
elsewhere. `include_router()` copies already-built `APIRoute` objects; it does
not reconstruct them under the app's `route_class`.

This is not merely "the route stays unwired" — it is a hard startup failure.
FastAPI validates and builds a route's parameters (as a Pydantic field, absent
a `Depends(...)`) at *decoration time*, i.e. the instant `@router.get(...)`
runs. A route decorated before its router was wired still carries a bare
`Autowired[T]` annotation, which FastAPI cannot treat as a valid Pydantic field
type, and raises `FastAPIError: Invalid args for response field!` immediately
— often during import, before the app object even exists.

**Goal:** calling `wire(app)` once must be sufficient, regardless of how many
`APIRouter` instances exist, when they were created, when their routes were
decorated relative to the `wire(app)` call, or whether any of them were ever
individually wired.

## Non-goals

- Per-router container overrides (e.g. a sub-router using a different
  container than the app it's mounted on) are explicitly out of scope. If a
  future need arises, it can be layered on top of `app.state` (e.g. a
  dedicated sub-app) without revisiting this design.
- No change to how `Autowired[T]` behaves for plain (non-FastAPI) field or
  constructor injection — `markers.py` and `container.py` are untouched.
- No change to `Container`'s resolution/singleton semantics.

## Current mechanism (being replaced)

- `wire(target: FastAPI | APIRouter, ...)` sets `target.router.route_class`
  (or `target.route_class` for a bare router) to a `functools.partial` of
  `_WiredRoute`, a custom `APIRoute` subclass.
- `_WiredRoute.__init__` calls `_wire_endpoint(endpoint, container)` — which
  eagerly rewrites bare `Autowired[T]` parameters into `T = Depends(...)`,
  with `container` baked in via `functools.partial(container.resolve, T)` —
  before delegating to `APIRoute.__init__`.
- This only works because `route_class` is read by `APIRouter.add_api_route`
  at the moment each route is built, and only for routes built by *that*
  router instance, after `route_class` was set on it.

This mechanism, `_WiredRoute`, and the `route_class` swap are removed
entirely by this redesign.

## Chosen approach

Patch `fastapi.routing.APIRouter.add_api_route` once, at `pywire.fastapi`
module import time (not inside `wire()`). This is the single choke point
every `@router.get/post/put/...`-style decorator — and `include_router()`
internally — passes through to construct a route. Rewriting bare
`Autowired[T]` parameters into `Depends(...)` at this point, before
delegating to the original implementation, means no route, on any router,
ever reaches FastAPI's field-building logic with a bare `Autowired[T]`
annotation — independent of `wire()` call order, target identity, or whether
`wire()` was ever called at all for that specific router.

Actual container resolution is deferred to **request time**: the `Depends(...)`
callable takes `request: Request` and reads `request.app.state.pywire_container`
(falling back to `get_default_container()` if unset), rather than a container
baked in at decoration time. This is what allows decoration to always be
safe regardless of whether `wire(app, container=...)` has run yet — the
container only needs to exist by the time the first *request* comes in, not
by the time routes are decorated.

### Alternatives considered

- **Patch `fastapi.routing.APIRoute.__init__` directly**, instead of
  `add_api_route`. Functionally equivalent (every route ends up going through
  `APIRoute.__init__` regardless), but reaches one layer deeper into
  FastAPI's internals and has a wider blast radius (would also affect any
  code that constructs `APIRoute` objects directly, bypassing
  `add_api_route` — an obscure but real pattern). `add_api_route` is the
  method FastAPI's own `router.get()`/`router.post()`/etc. call, making it
  the more official extension point. Rejected in favor of the chosen
  approach for a narrower, more predictable patch surface.
- **A `wired_router()`/`wired_app()` factory** wrapping `APIRouter()`/
  `FastAPI()` construction with a pre-set `route_class`, with no global
  patching. Rejected: still requires explicit per-router adoption, which is
  exactly the constraint this redesign exists to remove.
- **Keep `wire(router, container=...)` as an eager per-router override**,
  captured at decoration time when present. Rejected (explicitly, by the
  user) in favor of simplifying `wire()` to accept only `FastAPI` — the
  primary use case is fully covered by request-time resolution via
  `request.app.state`, and a per-router override is a rare enough need to
  defer until it's actually requested.
- **A module-level `WeakKeyDictionary[FastAPI, Container]`** as the
  app→container registry. Rejected in favor of `app.state.pywire_container`:
  `app.state` is FastAPI/Starlette's own supported extension point for
  exactly this kind of app-scoped value, is already unique per `FastAPI`
  instance, requires no separate lifecycle/GC reasoning, and is inspectable
  by users (e.g. in a debugger or test) without knowing pywire's internals.

## Detailed design

### Public API change

```python
def wire(app: FastAPI, *, container: Container | None = None) -> FastAPI:
    """Associate container with app for Autowired[T] route parameter resolution.

    Safe to call at any point relative to route/router decoration — decorating
    a route with a bare Autowired[T] parameter never fails, on any router,
    whether or not wire() has been called yet. If wire() is never called for
    an app, Autowired[T] parameters resolve against the module-level default
    container (the same one @component uses).

    Raises:
        TypeError: if app is not a FastAPI instance.
    """
```

This is a breaking change from the current `FastAPI | APIRouter` signature:
`wire()` no longer accepts a bare `APIRouter`. Calling `wire(some_router)`
now raises `TypeError`.

### Components (`src/pywire/fastapi.py`)

- **`_install_patch() -> None`** — patches `APIRouter.add_api_route`,
  guarded by a marker attribute on the wrapper (e.g.
  `_patched_add_api_route.__pywire_patched__ = True`, checked before
  patching) so re-running the module body (module reload) never double-wraps.
  Called once at module import time — a top-level call in `fastapi.py`, not
  inside `wire()`.
- **`_patched_add_api_route(self, path, endpoint, **kwargs)`** — calls
  `_wire_endpoint(endpoint)`, then delegates to the original
  `add_api_route(self, path, endpoint, **kwargs)`. `self` (the `APIRouter`
  instance) is not otherwise used; the patch is deliberately target-agnostic.
- **`_wire_endpoint(func) -> Any`** — same parameter-inspection logic as
  today (`get_type_hints` + `resolve_autowired_type` per parameter), but the
  `Depends(...)` it installs is always the lazy resolver — no container
  parameter anymore. Naturally idempotent: a parameter already rewritten
  (annotation is a plain type, not `Autowired[...]`) is skipped on a second
  pass, which is what makes it safe for `include_router()` to trigger
  `add_api_route` a second time on the parent router for an already-wired
  endpoint.
- **`_resolve_autowired(target: type) -> Callable[[Request], Any]`** —
  returns a closure `resolver(request: Request)` that does
  `getattr(request.app.state, "pywire_container", None) or
  get_default_container()`, then `container.resolve(target)`.
- **`wire(app, *, container=None)`** — validates `isinstance(app, FastAPI)`
  (else `TypeError`), sets `app.state.pywire_container = container or
  get_default_container()`, returns `app`.

Removed entirely: `_WiredRoute`, the `route_class`/`cast` machinery, the
`APIRouter` import (no longer needed since `wire()` no longer branches on it
— though `add_api_route` is still patched *on* `APIRouter`, so the import
for patching purposes remains; `wire()` itself just stops accepting it as a
parameter type).

### Data flow

1. `pywire.fastapi` is imported (typically near the top of the app's entry
   module, before any router module is imported) → `_install_patch()` runs
   once, patching `APIRouter.add_api_route` for the rest of the process.
2. Any `APIRouter` is created and decorated, anywhere, in any order, wired
   or not → each `@router.get(...)` passes through the patched
   `add_api_route` → bare `Autowired[T]` parameters become
   `Depends(lazy_resolver)` before FastAPI builds/validates the route → no
   decoration-time crash, ever.
3. `app.include_router(router)` copies routes onto the app's router,
   internally re-invoking `add_api_route` on the parent — a no-op pass
   through `_wire_endpoint` since the endpoint is already rewritten.
4. `wire(app, container=X)` (called whenever convenient — before or after
   step 2/3) sets `app.state.pywire_container = X`.
5. A request arrives → FastAPI resolves the endpoint's `Depends(...)` →
   `lazy_resolver(request)` reads `request.app.state.pywire_container`
   (or the default container if `wire()` was never called for this app) →
   `container.resolve(target)`.

### Error handling

- `wire(x)` where `x` is not a `FastAPI` instance → `TypeError`, unchanged
  in spirit from the current guard (message text updated to drop the
  `APIRouter` mention).
- A type that was never registered in the resolved container still fails
  exactly as `Container.resolve()` already fails today — this redesign does
  not touch that behavior.
- No new failure modes are introduced; the whole point of this redesign is
  removing the decoration-time failure mode.

## Testing

- `tests/test_fastapi_integration.py`:
  - Rewrite `test_wire_router_before_decoration_supports_include_router_pattern`
    to match the actual bug report: build the `APIRouter`, decorate its
    route directly (no `wire(router, ...)` call at all — it no longer
    exists), *then* create the `FastAPI` app, call `wire(app, ...)`, *then*
    `include_router()`. This reproduces the exact scenario from the bug
    report and must pass.
  - Add a test that decorates a route on a plain `APIRouter` *before*
    `FastAPI()` even exists (no app to wire yet at decoration time), then
    creates and wires the app afterward, then includes the router — proving
    decoration never depends on an app existing yet.
  - Add a test that never calls `wire()` at all and confirms `Autowired[T]`
    still resolves via the default container (register the component with
    `get_default_container()` or `@component` beforehand).
  - Update `test_wire_rejects_invalid_target` (and remove the now-invalid
    `APIRouter`-is-a-valid-target assumption anywhere it appears) to assert
    `TypeError` for a non-`FastAPI` argument, including passing a bare
    `APIRouter()` explicitly (this is now an invalid target, not a
    supported one).
  - Existing tests that already call `wire(app, container=...)` then
    `@app.get(...)` must keep passing unchanged in behavior (they exercise
    the lazy path transparently).
- No changes needed in `tests/test_container.py` or
  `tests/test_constructor_injection.py` — this redesign is scoped entirely
  to `fastapi.py`.

## Documentation updates required

- `README.md`: "Usage" section drops the "(or an `APIRouter`)" phrasing —
  `wire()` is called on the app only. "Limitation" section is rewritten:
  there is no longer an ordering limitation to document; replace with a
  short note on how `Autowired[T]` resolves (app.state, default-container
  fallback) instead of a caveat.
- `CLAUDE.md` → "FastAPI integration (`fastapi.py`)" section: full rewrite
  to describe the `add_api_route` patch, `_resolve_autowired`, and
  `app.state.pywire_container`, replacing the `_WiredRoute`/`route_class`
  description and the `wire(router)`-per-module caveat.

## Migration notes (breaking change)

Existing callers doing `wire(router, container=...)` on a bare `APIRouter`
must switch to calling `wire(app, container=...)` once, on the `FastAPI`
instance, at any point in `create_app()`. No other code changes are
required — router modules and their `@router.get(...)` decorators are
unaffected either way, since the previous explicit `wire(router)` call
becomes simply unnecessary rather than replaced by something else.
