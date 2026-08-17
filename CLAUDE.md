# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

PyWire is a minimal Dependency Injection container for Python 3.13, inspired by Spring's
`@Component`/`@Autowired`. It is a small library (no runtime dependencies) living entirely
under `src/pywire/`.

## Commands

Package management is `uv`-based (`uv.lock` present).

```bash
uv sync                # install dependencies (incl. dev group)
uv run pytest          # run the full test suite
uv run pytest tests/test_container.py::test_register_and_resolve   # run a single test
uv run ruff check .    # lint
uv run pyright         # type-check
./scripts/bump-version.sh major|minor|patch   # bump version, commit, and tag (see below)
```

There are no `[project.scripts]` entries — this is a library, not a CLI. Maintainer-only
tooling (versioning) lives in `scripts/`, outside `src/pywire/`, so it never ships as part
of the built package.

### Bumping the version

Run `./scripts/bump-version.sh major|minor|patch` only when the user explicitly asks for a
version bump/release — never run it proactively as part of an unrelated task. Requirements
and behavior:

- The working tree must be clean (`git status --porcelain` empty); the script aborts
  otherwise. Do not stash or commit unrelated changes just to force it through — surface the
  dirty state to the user instead.
- It bumps `pyproject.toml` via `uv version --bump <part>`, re-locks `uv.lock`, commits both
  as `chore: bump version to X`, and creates local tag `vX`. It never pushes.
- After it runs, ask the user before pushing — `git push && git push --tags` is the follow-up
  command, but pushing commits/tags is a shared-state action and needs explicit confirmation
  per this session's operating rules, even though the script itself never pushes.

## Architecture

### Registration & resolution flow

- `Container` (`container.py`) owns a private `_registry: dict[type, BeanDefinition]`.
  Each `Container` instance is an independent scope: the same class registered in two
  different containers produces two different singleton instances.
- `container.register(cls)` stores a `BeanDefinition` (`definitions.py`) and immediately
  calls `self._instrument(cls)` — it does **not** instantiate the class yet.
- `container.resolve(cls)` / `container.get(cls)` (alias) lazily creates the singleton
  on first access and returns the cached instance afterward.

### Field injection mechanism (`Container._instrument`)

This is the core trick of the library and the most important thing to understand before
touching `container.py`:

- `Autowired[T]` (`markers.py`) is a PEP 695 type alias, `type Autowired[T] =
  Annotated[T, _AUTOWIRED]`, where `_AUTOWIRED` is a private sentinel. Static type checkers
  see the field as plain `T`. At runtime, subscripting the alias (`Autowired[Foo]`) does
  **not** collapse to `Annotated[Foo, _AUTOWIRED]` — `typing.get_origin` on it returns the
  `Autowired` alias itself, not `Annotated`. The container checks `get_origin(annotation) is
  Autowired` and pulls `T` out via `get_args` (single-element tuple, no metadata to filter —
  the origin check alone proves it's an Autowired field).
- `_instrument()` monkey-patches `__new__` and `__init__` on every registered class:
  - The patched `__new__` writes the freshly created (but not yet `__init__`-ed) instance
    into `BeanDefinition.instance` **before** running `__init__`. This early registration is
    what makes circular dependencies resolvable — see below.
  - The patched `__init__` walks `inspect.get_annotations(cls, eval_str=True)`, resolves each
    `Autowired[...]` field (via `get_origin`/`get_args`; a string forward reference inside
    `Autowired["X"]` surfaces as a plain `str`, not a `ForwardRef`, under the PEP 695 alias,
    and is evaluated against the owning module's globals) to a concrete type, and injects the
    resolved dependency via `self.resolve(field_type)` before calling the original `__init__`.
- Circular dependencies (A depends on B, B depends on A) are handled via two instance flags
  set on each object: `_di_initializing` and `_di_initialized`. If `__init__` is re-entered
  on an instance that is already mid-construction (found via the early registry write in
  `__new__`), it returns immediately, leaving the partially-constructed instance to be wired
  up on an outer stack frame. See `tests/test_circular_dependencies.py` for the exact
  scenarios this supports (mutual references, self-reference, forward-ref strings).
- The `get_origin`/`get_args` extraction described above is not inlined in `container.py`:
  it lives in `markers.py` as `resolve_autowired_type(annotation, module_globals)`, shared
  verbatim between field injection (above) and constructor injection (below).

### Constructor injection mechanism (`Container._instrument`)

Constructor parameters annotated `Autowired[T]` are resolved through the same
`resolve_autowired_type` helper as fields, but the annotations are gathered differently
because `inspect.get_annotations(cls, ...)` only sees class-level attributes, not `__init__`
parameters:

- At registration time, `_instrument()` computes
  `init_hints = get_type_hints(original_init, include_extras=True)` once, then intersects it
  with `inspect.signature(original_init).parameters` (skipping `self`) to build
  `ctor_autowired_params: dict[str, type]`.
- This `get_type_hints()` call deliberately passes **no explicit `globalns`**. `original_init`
  may be inherited from a base class defined in a different module than `cls`, so
  `cls.__module__` would be the wrong module to resolve forward references and string
  annotations against. `get_type_hints()` with no explicit `globalns` already resolves
  correctly on its own, since it reads `original_init.__globals__` internally — which is
  always the module `__init__` was actually defined in, regardless of which subclass is being
  instrumented.
- The patched `__init__` resolves each entry in `ctor_autowired_params` via `self.resolve(...)`
  and passes it as a keyword argument to `original_init`, but only for parameter names not
  already present in the caller-supplied `kwargs` — an explicitly passed keyword argument
  always wins over auto-resolution.
- Field injection and constructor injection can coexist on the same class: the field loop
  (`setattr`) runs first, then the resolved constructor kwargs are computed and passed into
  `original_init`.
- Circular-dependency nuance specific to constructor injection: in an A↔B cycle, the partner
  instance obtained via `self.resolve(...)` while resolving the other side's constructor
  arguments is the **not-yet-initialized** instance — it was early-registered by `__new__`,
  but its own `__init__` has not completed, since resolving *its* constructor arguments is
  still in progress on an inner stack frame. This is analogous to, but not identical to, the
  field-injection case, where some of the partner's fields may already be `setattr`-set by the
  time the circular call resolves it. See `tests/test_constructor_injection.py` for the exact
  scenario (`CircularA`/`CircularB`) this supports.

### Component decorators (`decorators.py`)

- `component` (and its aliases `service`, `repository`, `agent`, `client` — currently pure
  synonyms with no distinct behavior) always registers a class against a lazily-created
  module-level default container (`get_default_container()`). It takes no container
  argument by design — use `container.register(cls)` directly when an explicit container
  is needed.

### FastAPI integration (`fastapi.py`)

Optional module (requires the `fastapi` extra, `uv pip install -e ".[fastapi]"`) that lets route
handlers declare dependencies as bare `Autowired[T]` parameters instead of manual
`Depends(...)` wiring. It only imports `fastapi`, never the reverse — `container.py` has no
knowledge of this module.

- The module patches `fastapi.routing.APIRouter.add_api_route` once, at import time (a
  top-level `_install_patch()` call, not something `wire()` triggers) — not per-target like the
  old `route_class` mechanism. `add_api_route` is the single choke point every
  `@router.get/post/put/...`-style decorator passes through to build an `APIRoute`, so patching
  it intercepts every route, on every `APIRouter`, independent of `wire()` call order, target
  identity, or whether `wire()` was ever called for that specific router at all. (HTTP routes
  only — `add_api_websocket_route` is not patched, so WebSocket routes are not covered; this is
  not a regression, the old design didn't cover WebSockets either.)
- This does require `pywire.fastapi` itself to be imported before any module that decorates a
  route with `Autowired[T]` — e.g. `from pywire.fastapi import wire` near the top of the app's
  entrypoint, before importing router modules. The global patch above is installed at
  `pywire.fastapi`'s own import time; if a router module is imported first, with
  `pywire.fastapi` not yet in `sys.modules`, the original decoration-time `FastAPIError` this
  redesign eliminates can still occur.
- `_install_patch()` guards against double-wrapping: it checks
  `getattr(APIRouter.add_api_route, "__pywire_patched__", False)` before patching, and returns
  immediately if already set. `_patched_add_api_route` is defined as a closure *inside*
  `_install_patch()`, capturing the current (unpatched, on first call) `add_api_route` as
  `original` — this closure-per-call-attempt design, rather than a module-level mutable
  "original" variable, is what keeps re-running the module body (e.g. via `importlib.reload`)
  safe: a second `_install_patch()` call either finds the guard already set and does nothing, or
  (if it somehow ran on a still-unpatched `APIRouter`) captures a fresh, correct `original`.
- `_patched_add_api_route(self, path, endpoint, **kwargs)` calls `_wire_endpoint(endpoint)` — no
  container involved — then delegates to `original(self, path, endpoint, **kwargs)`.
- `_wire_endpoint(func)` reads `get_type_hints(func, include_extras=True)`, and for every
  parameter whose annotation resolves via `resolve_autowired_type` to some `target` type,
  replaces that parameter's `inspect.Parameter` with one where `annotation=target` and
  `default=Depends(_resolve_autowired(target))`. It then reassigns `func.__signature__` to the
  rewritten `inspect.Signature` — the `__signature__` rewrite trick. FastAPI's own
  request-parsing introspects `__signature__`/`get_type_hints` on the endpoint to decide how to
  satisfy each parameter; rewriting it before `add_api_route` builds the `APIRoute` is what
  turns a bare `Autowired[Service]` parameter into an ordinary FastAPI `Depends(...)` parameter
  from FastAPI's point of view. Parameters that are not `Autowired[...]` are left untouched.
- `_resolve_autowired(target)` returns a closure `resolve(request: Request) -> Any` that reads
  `request.app.state.pywire_container` (falling back to `get_default_container()` if unset) and
  calls `container.resolve(target)`. Container resolution is deferred to **request time**, not
  baked in at decoration time — this is what allows decoration to always be safe regardless of
  whether `wire(app, container=...)` has run yet: the container only needs to exist by the time
  the first *request* comes in, not by the time routes are decorated.
- `wire(app: FastAPI, *, container: Container | None = None) -> FastAPI` requires `app` to be a
  `FastAPI` instance — it raises `TypeError` otherwise, including for a bare `APIRouter`, which
  is no longer a supported target. It sets `app.state.pywire_container = container or
  get_default_container()` and returns `app`. `app.state` is Starlette's own idiomatic
  per-app-instance storage extension point — no separate module-level registry needed.
- **No more ordering limitation** (see the import-order precondition above): because every
  route is rewritten at decoration time regardless of `wire()` call order, `wire(app)` can run
  at any point relative to route/router decoration — before, after, or interleaved. If `wire()`
  is never called for an app at all, `Autowired[T]` parameters resolve against the module-level
  default container (the same one `@component` uses), via the `_resolve_autowired` fallback
  above — silently: a forgotten `wire(app, container=...)` does not fail, it just resolves
  against a container that may not have the expected component registered. See
  `tests/test_fastapi_integration.py`.

### Versioning (`scripts/bump-version.sh`)

- `scripts/bump-version.sh <major|minor|patch>` bumps `pyproject.toml`'s `version` via
  `uv version --bump <part>` (which also re-locks `uv.lock`), then commits both files
  as `chore: bump version to X` and creates a local git tag `vX`. It refuses to run on
  a dirty working tree and never pushes — push explicitly with `git push && git push --tags`.
- Lives in `scripts/` at the repo root, not under `src/pywire/`, so it's maintainer-only
  tooling and never ships inside the built package.

### Module layout

| File | Responsibility |
|---|---|
| `container.py` | `Container`: registry, resolve/register, `__new__`/`__init__` instrumentation |
| `definitions.py` | `BeanDefinition` (registration metadata) and `Scope` enum (only `SINGLETON` is implemented; `PROTOTYPE` is declared but unused) |
| `decorators.py` | `@component` and aliases, global container accessor |
| `markers.py` | `Autowired[T]` (PEP 695 alias of `Annotated[T, _AUTOWIRED]`) and shared `resolve_autowired_type()` |
| `exceptions.py` | `DependencyResolutionError` |
| `fastapi.py` | Optional FastAPI integration: `wire()`, `_install_patch`, `_wire_endpoint`, `_resolve_autowired` — resolves bare `Autowired[T]` route parameters via a global `add_api_route` patch |

## Conventions to preserve

- All docstrings, comments, and error/exception messages are written in English only.
- `ruff` config (`pyproject.toml`) enables `E, F, I, UP, RUF` rule sets, target `py313`,
  line length 88, first-party import group `pywire`.
