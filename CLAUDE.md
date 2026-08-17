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

Optional module (requires the `fastapi` extra, `pip install pywire[fastapi]`) that lets route
handlers declare dependencies as bare `Autowired[T]` parameters instead of manual
`Depends(...)` wiring. It only imports `fastapi`, never the reverse — `container.py` has no
knowledge of this module.

- `wire(target: FastAPI | APIRouter, *, container: Container | None = None) -> FastAPI |
  APIRouter` resolves `router = target.router if isinstance(target, FastAPI) else target`,
  then sets `router.route_class = functools.partial(_WiredRoute, container=container)` (cast
  to `type[APIRoute]` for the type checker's benefit, since a `functools.partial` is not
  literally a `type[APIRoute]` subclass, only callable like one) and returns `target`
  unchanged.
- `route_class` is FastAPI's own extension point on `APIRouter`: it's the callable the router
  uses to build every `APIRoute` object for a path operation registered from that point
  onward. Overriding it is what lets pywire intercept each endpoint's signature without
  patching FastAPI itself.
- `_WiredRoute(APIRoute)` overrides `__init__` to call
  `_wire_endpoint(endpoint, container or get_default_container())` before delegating to
  `APIRoute.__init__` — so the fallback to the default container is evaluated per route, at
  the moment each route is registered (i.e. every time a `@app.get(...)`-style decorator
  runs), not once at `wire()` call time.
- `_wire_endpoint(func, container)` reads `get_type_hints(func, include_extras=True)`, and for
  every parameter whose annotation resolves via `resolve_autowired_type` to some `target`
  type, replaces that parameter's `inspect.Parameter` with one where `annotation=target` and
  `default=Depends(functools.partial(container.resolve, target))`. It then reassigns
  `func.__signature__` to the rewritten `inspect.Signature` — the `__signature__` rewrite
  trick. FastAPI's own request-parsing introspects `__signature__`/`get_type_hints` on the
  endpoint to decide how to satisfy each parameter; rewriting it before `APIRoute.__init__`
  runs is what turns a bare `Autowired[Service]` parameter into an ordinary FastAPI
  `Depends(...)` parameter from FastAPI's point of view. Parameters that are not
  `Autowired[...]` are left untouched.
- **Limitation**: `wire()` only affects routes registered *after* it runs, because it works
  by swapping `route_class` on the router — routes already built keep whatever `route_class`
  was active when they were constructed. Call `wire(app)` (or `wire(router)`) immediately
  after creating the app/router, before any `@app.get(...)` / `@router.get(...)` calls. Each
  `APIRouter` instance needs its own `wire()` call; wiring the main `FastAPI` app does not
  propagate to routers mounted onto it via `include_router()`, since each router's routes are
  built with whichever `route_class` was set on that specific router object at definition
  time. See `tests/test_fastapi_integration.py`.

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
| `fastapi.py` | Optional FastAPI integration: `wire()`, `_WiredRoute`, `_wire_endpoint` — resolves bare `Autowired[T]` route parameters |

## Conventions to preserve

- All docstrings, comments, and error/exception messages are written in English only.
- `ruff` config (`pyproject.toml`) enables `E, F, I, UP, RUF` rule sets, target `py313`,
  line length 88, first-party import group `pywire`.
