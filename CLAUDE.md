# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

PyWire is a minimal Dependency Injection container for Python 3.13, inspired by Spring's
`@Component`/`@Autowired`. It is a small library (no runtime dependencies) living entirely
under `src/pywire/`.

## Discussion protocol

For broad-scope discussions in this project — new features, architectural decisions,
ambiguous or open-ended requirements — invoke both `superpowers:brainstorming` and
`mattpocock-skills:grilling`: brainstorming to explore intent and design, grilling to
stress-test the result before implementation starts.

Surface every clarifying question through the `AskUserQuestion` tool, one question per
call — wait for the answer before asking the next, never batch several questions into a
single call.

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
- `container.register(cls)` stores a `BeanDefinition` (`definitions.py`) — it does
  **not** instantiate or modify the class in any way.
- Three registration APIs share one choke point, `Container._put`, which owns the
  "already registered" rule: `register(cls, as_type=None)`,
  `register_instance(obj, as_type=None)`, `register_factory(target_type, factory)`.
  `as_type` **rebinds** the key rather than adding one — one registration, one key
  — and the binding is **not** checked, by anything. `as_type: type[T]` would only
  widen `T` to the join of both arguments and accept an unrelated class (measured on
  this project's pyright), so the annotation is a bare `type | None`; `issubclass`
  cannot check a structural `Protocol` either, and a `Protocol` is the main reason
  the parameter exists. A wrong binding surfaces as an `AttributeError` on the
  resolved object.
- `BeanDefinition.factory` decides how a bean is obtained: `None` means construct
  `cls`, non-`None` means call the factory and publish what it returns.
  `register_instance(obj)` is the factory path with a named closure returning `obj`
  — which is why `clear_instances()` and `_roll_back` need no special case: every
  bean is rebuildable, and rebuilding a pushed one yields the same object.
  `BeanDefinition.origin` records which of the three it was, and is read by humans
  only.
- A factory publishes **late** — the object does not exist until it returns — so a
  cycle closing on a factory bean can never find a partial instance. No new
  mechanism handles this: a `resolve()` called from inside a factory sees a
  non-empty stack and is a `CTOR` edge, which the existing cycle rule rejects. The
  `instance is None` check on the legal-cycle branch is an invariant check against
  a future fourth `_EdgeKind`, not a reachable path.
- Two runtime rejections, both preventing a value that is not an object from
  becoming a bean: `register_instance(None)` and a coroutine function passed to
  `register_factory`. Nothing else about a binding is checked at all, by the
  container or by a type checker.
- `container.resolve(cls)` / `container.get(cls)` (alias) lazily creates the singleton
  on first access and returns the cached instance afterward.

### Injection mechanism (`Container.resolve`)

`register()` records; `resolve()` constructs. A registered class is **never** modified
— no `__new__`/`__init__` patching, no attributes written onto user instances. This is
the most important thing to understand before touching `container.py`.

`resolve()` first tries an unsynchronised fast path: if the definition's `ready` flag is
set, the singleton is complete and is returned without taking the lock. Otherwise it
takes the `RLock`, creates a `_Resolution` if it is the outermost call (storing it in
`self._current` so a reentrant call from a component's `__init__` finds it), and
delegates to `_resolve(target, edge, requester, resolution)`. The extra arguments thread
*edge kinds* and the *requester label* through the recursion without changing the public
signature. `_resolve` does:

1. look the `BeanDefinition` up, else `DependencyResolutionError`
2. if the type is already on `resolution.stack`, a cycle is closing: reject it if any
   edge from its position forward — **plus the incoming edge** — is a constructor edge,
   otherwise return the partial instance. If it is not on the stack and
   `definition.instance` is set, that is an ordinary cache hit
3. record `len(resolution.created)`, push `(type, edge)` onto `resolution.stack`
4. compute and cache `definition.plan` if absent — **before** allocating, so an
   unplannable class is never created. Plan errors are re-raised via
   `PyWireError.with_context()` to pick up the chain
5. `type.__new__(type)`
6. publish `definition.instance` and append to `resolution.created` — **early
   publication**, what lets a field cycle close instead of recursing forever
7. inject every planned field with `setattr` — **before `__init__`**, which is a
   contract: a component's `__init__` body can read its injected fields
8. resolve the planned constructor arguments
9. `type.__init__(instance, **kwargs)`
10. set `definition.ready = True` — success path only, and only while
    `definition.instance` is still the object this frame built; this is what
    the fast path reads
11. on exception: roll back to the mark from step 3
12. finally: pop `resolution.stack`. The `_Resolution` itself is discarded by the
    outermost `resolve()`, so there is no separate "clear when it empties" step

**Where the state lives.** `_Resolution` (stack + created log) is *call-scoped*, not
container state: the previous design kept `_di_initializing` flags on user instances,
and keeping the equivalent lists on `Container` would repeat that category error with a
different victim. The one exception is `Container._current`, which holds the resolution
in flight — necessary because `resolve()`'s public signature is fixed, so a reentrant
call made from inside an `__init__` has no other way to find the stack it belongs to.
Safe as a single field precisely because the lock admits one thread.

**Cycle policy.** A cycle is legal only if *every* edge is a field edge. The rationale
is mechanical, not contractual: a constructor parameter must be resolved before
`__init__` can be called, so a constructor cycle has no fixed point, whereas a field
can be assigned to an already-allocated object. The check lives at step 2 and inspects
the *cycle*, not the current frame — checking the frame would make a mixed
field/constructor cycle succeed or fail depending on which type was resolved first,
since only one entry point reaches the cycle through the constructor edge. A
`resolve()` called by hand from inside an `__init__` counts as a constructor edge.
`_EdgeKind` is a plain `Enum`; `ROOT` and `FIELD` are behaviourally identical and kept
apart only for readability in a printed stack.

Caveat to keep in mind: in a legal field cycle the partner's `__init__` observes a
partially-wired object. Field injection running before `__init__` is what makes that
true, and it is the price of the contract in step 7.

**Errors carry context by construction, not by mutation.** `PyWireError` is immutable:
`chain` comes from the stack at the raising frame, and `requester` is passed *down* the
recursion as an argument, so the frame that raises already knows both. `str(exc)`
therefore never changes as the exception propagates. `plans.py` is pure inspection and
cannot know the chain, so `Container._plan` re-raises its errors through
`with_context()`, which returns a same-typed copy and never overwrites context the
raising frame already supplied.

**Rollback.** Every frame records `len(resolution.created)` on entry and, on exception,
truncates back to it, clearing `instance` **and `ready`** on each definition dropped.
Per-subtree rather than outermost-only, because a component whose `__init__` resolves an
optional dependency inside `try/except` swallows the failure before any outer frame sees
it — outermost-only rollback would leave those partials cached forever. Leaving `ready`
set would be worse still: the fast path would keep handing out a disowned bean.
`definition.plan` is never cleared — a plan is a pure function of the class.

**Reentrant `clear_instances()`.** The lock is reentrant, so a component whose
`__init__` calls `container.clear_instances()` wipes the definition its own frame is
building. Step 10 is conditional for that reason: publishing `ready` over an emptied
definition would leave `ready=True` with `instance=None` — the one combination the fast
path cannot detect — and every later `resolve()` would return `None` cast to `T`,
silently and permanently. Such a bean is left uncached instead; the caller still
receives the object its frame built, and the next `resolve()` rebuilds.

**Lazy planning.** `definition.plan` is computed on first resolution, not at
registration. Registration therefore cannot fail because of an annotation unrelated to
injection, and `Autowired["X"]` where `X` is defined later in the module resolves
correctly. The cache lives on `BeanDefinition`, so two containers plan the same class
twice; a global memo was rejected as process-wide mutable state in a redesign whose
thesis is the absence of it.

**Thread safety.** A single `threading.RLock` guards construction — reentrant because
resolution recurses. Held coarsely, across user `__init__` bodies, because that is what
guarantees one singleton per type: releasing it around `__init__` would let two threads
both miss the cache and both build. Consequences: user `__init__` code runs under the
lock, so a component that waits on another thread's `resolve()` deadlocks; rollback
restores the registry but cannot undo `__init__` side effects, which a retry re-runs; and
the `ready`-gated fast path relies on the GIL ordering the writes in steps 6 and 10, so a
free-threaded build would need an explicit acquire/release pair.

**Consequences to keep in mind.** `Cls()` written by hand is plain Python and is *not*
wired — its `Autowired` fields are absent. Subclasses of a registered component are
ordinary classes, and an `Autowired` field declared on a base **is** injected into a
registered subclass unless re-annotated without `Autowired`. `__slots__` components work
as long as they declare no injected field — that one is caught at injection time rather
than plan time, because a base class may supply `__dict__`, so it is not statically
decidable. A dataclass's `Autowired` field is a constructor parameter, not a field, so
frozen dataclasses work. Classes whose `__new__` requires arguments, frozen classes with
a genuinely field-injected dependency, positional-only `Autowired` parameters, and
non-defaulted non-`Autowired` parameters all fail with an explicit
`UnconstructibleComponentError`.

### Annotation evaluation (`markers.py`)

One evaluator, one policy. `evaluate_annotation()` is **total**: it never raises. An
unresolvable name becomes a `_MissingName` placeholder (via a `dict` subclass passed as
`eval` *locals*, which delegates to real globals and builtins before inventing
anything), and any other evaluation failure yields one placeholder for the whole
expression. That is what lets a single broken annotation coexist with valid `Autowired`
fields on the same class.

`resolve_autowired_type()` is then the single site that decides what a failure means,
with a three-case contract: `Autowired[T]` resolvable → `T`; `Autowired[T]` unresolvable
→ `AnnotationResolutionError` (returning `None` would be indistinguishable from "not
Autowired" and would silently skip the injection); anything else → `None`, *including* a
non-`Autowired` annotation that itself contains an unresolvable name, such as a
`TYPE_CHECKING`-only import.

`callable_hints()` wraps `get_type_hints()` with a per-annotation fallback through
`evaluate_annotation`, and is shared by `plans.py` and `fastapi.py` — which is why
`fastapi.py` needs no knowledge of `plans.py`.

### Component decorators (`decorators.py`, `aliases.py`)

- `component` always registers a class against a lazily-created module-level default
  container (`get_default_container()`, defined on `container.py` — see below). It takes
  no container argument by design — use `container.register(cls)` directly when an
  explicit container is needed. It is dual-form: bare (`@service`) or called with a
  required `as_type` keyword (`@repository(as_type=UserRepository)`), which rebinds the
  key exactly as `Container.register` does. `@component()` with empty parentheses is a
  `TypeError`. On this path the subtype relation is *not* checked statically: preserving
  the decorated class's own type for callers is worth more, and Python cannot express
  both.
- `aliases.py` binds `service`, `repository`, `agent`, `client`, and `provider` to
  `component` — currently pure synonyms with no distinct behavior, kept in their own
  module so `decorators.py` stays limited to `component` itself.
- `get_default_container()` (`container.py`) initialises that global under a
  module-level `Lock`, with a double check: the outer, unsynchronised test keeps the
  steady state free, and the inner one is what stops threads racing the very first
  `@component` from each building their own container and overwriting the winner's.
  That loss would be silent — `@component` returns the class either way, so it would
  only surface later as a failed `resolve()`. A plain `Lock`, not an `RLock`: nothing
  reachable from `Container()` calls back in.

### Lifecycle teardown (`lifecycle.py`, `Container.close()`)

Two declaration surfaces, normalized at registration time into one
`BeanDefinition.teardown: Callable[[Any], None] | None` -- `Container` never branches
on which one a bean used. `@pre_destroy` marks an instance method (pure tag, no
wrapping) for a class you own; `find_pre_destroy()` walks the MRO most-derived first,
and a `seen` set ensures only the first (most derived) definition of a given method
name is ever inspected -- a subclass that overrides the method without re-decorating
it is that first definition, is unmarked, and the base's marked version is never
reached, opting the class out. Same rule already documented for `Autowired` fields on
a base class.
`on_close=` on `register`/`register_factory`/`register_instance` covers everything
else; the two are mutually exclusive for the same bean, rejected eagerly by
`resolve_teardown()` at registration, never resolved with a silent precedence rule.
Both a `@pre_destroy` method's signature and an `on_close` callable's are validated
eagerly too -- must accept the torn-down instance as their only required argument --
though `on_close` tolerates a callable `inspect.signature()` cannot introspect
(a lambda is always readable; a C-implemented callable might not be), falling back to
"assumed fine, let the call speak for itself", the same posture `plans.py` already
takes for an uninspectable `__new__`.

`Container._ready_order` needs no dependency graph: because a bean only reaches
`ready=True` after every dependency it needed was itself already resolved, the order
beans become ready *is* a valid topological order by construction. `close()`
destroys in reverse of it -- dependents before dependencies -- attempting every bean
regardless of earlier failures and aggregating what fails into one `ExceptionGroup`,
never logged-and-swallowed. Its lock is held only to capture `(definition, instance)`
pairs and reset container state; the teardown calls themselves run outside it, so a
slow teardown (a pool doing blocking I/O to shut down) cannot stall an unrelated
`resolve()` on another thread, and a teardown that reentrantly calls `resolve()` only
ever appends to a fresh, already-cleared `_ready_order`. `close()` leaves the registry
intact, exactly like `clear_instances()` -- there is no "closed" state, a `resolve()`
afterward simply rebuilds, and a second `close()` is a no-op.

`_roll_back` gets the identical treatment for a `resolve()` subtree discarded mid-
construction, with one filter `close()` does not need: only a definition that reached
`ready=True` *before* the failure is torn down. The bean whose own construction is
failing is always present in the same discarded slice -- its instance was published
early, before `__init__` ran -- but it never reached `ready=True`, so it is excluded
rather than handed a teardown hook expecting a fully-built object. Its order and
membership are derived from `self._ready_order` -- filtered down to the definitions
this subtree is discarding (matched by `id()`, not `==`, since two cleared
`BeanDefinition`s can become field-equal to each other) and removed from
`_ready_order` in that same pass -- rather than from `resolution.created`:
`resolution.created` records allocation-*start* order, appended the moment
`__new__` returns and before that bean's own dependencies are resolved, so for a
nested chain it is not completion order. `self._ready_order` is appended at the one
point `ready` is set, which *is* true completion order by construction, and reversing
the filtered list gives correct dependents-before-dependencies teardown order with no
separate graph needed; removing the matched entries from `_ready_order` in the same
pass is also what stops a bean that rolls back here and is later rebuilt successfully
from accumulating a second `_ready_order` entry that would fire its teardown twice on
a future `close()`. `_create`'s except clause combines -- never replaces -- the
exception that triggered the rollback with whatever `_roll_back`'s own teardown
attempts raised, picking `ExceptionGroup` or `BaseExceptionGroup` by whether the
triggering exception is itself an `Exception`: `_roll_back` runs even for a bare
`BaseException` like `KeyboardInterrupt`, which `ExceptionGroup` cannot hold.

No `atexit` hook and no duck-typing fallback (auto-detecting `close()`/`__exit__` on
an undeclared bean): a bean without either declaration simply has no teardown. No
runtime check ties `on_close`'s declared argument type to what actually gets resolved
-- the same trust boundary already accepted for `as_type`, `Callable[[Any], None]`
being the honest annotation once stored on `BeanDefinition`, same reasoning as
`as_type: type | None` being bare instead of `type[T]`.

### FastAPI integration (`fastapi.py`)

Optional module (requires the `fastapi` extra, `uv pip install -e ".[fastapi]"`) that lets route
handlers declare dependencies as bare `Autowired[T]` parameters instead of manual
`Depends(...)` wiring. It only imports `fastapi`, never the reverse — `container.py` has no
knowledge of this module.

- The module patches `fastapi.routing.APIRouter.add_api_route` once, at import time (a
  top-level `_install_patch()` call, not something a container-configuration call triggers) —
  not per-target like the old `route_class` mechanism. `add_api_route` is the single choke
  point every `@router.get/post/put/...`-style decorator passes through to build an `APIRoute`,
  so patching it intercepts every route, on every `APIRouter`, independent of when (or whether)
  the app's container is ever configured. (HTTP routes only — `add_api_websocket_route` is not
  patched, so WebSocket routes are not covered; this is not a regression, the old design didn't
  cover WebSockets either.)
- This does require `pywire.fastapi` itself to be imported before any module that decorates a
  route with `Autowired[T]` — e.g. `from pywire.fastapi import pywire_lifespan` near the top of
  the app's entrypoint, before importing router modules. The global patch above is installed at
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
- `_wire_endpoint` reads annotations through `markers.callable_hints`, so an unrelated
  unresolvable annotation on *any* endpoint in the process can no longer abort route
  registration. When `resolve_autowired_type` raises for an `Autowired[T]` parameter,
  the parameter is still rewritten — to `annotation=object` with
  `Depends(_resolve_autowired_late(...))` — and the resolution is retried on the first
  request, memoised thereafter. That keeps decoration unconditionally safe *and* makes
  endpoints as lazy as components: a route may inject a service defined further down
  its own module. A genuinely undefined name fails at request time, naming the endpoint.
- For every parameter whose annotation resolves via `resolve_autowired_type` to some `target`
  type, `_wire_endpoint` replaces that parameter's `inspect.Parameter` with one where
  `annotation=target` and `default=Depends(_resolve_autowired(target))`. It then reassigns
  `func.__signature__` to the rewritten `inspect.Signature` — the `__signature__` rewrite
  trick. FastAPI's own request-parsing introspects `__signature__`/`get_type_hints` on the
  endpoint to decide how to
  satisfy each parameter; rewriting it before `add_api_route` builds the `APIRoute` is what
  turns a bare `Autowired[Service]` parameter into an ordinary FastAPI `Depends(...)` parameter
  from FastAPI's point of view. Parameters that are not `Autowired[...]` are left untouched.
- `_resolve_autowired(target)` returns a closure `resolve(request: Request) -> Any` that reads
  `request.app.state.pywire_container` (falling back to `get_default_container()` if unset) and
  calls `container.resolve(target)`. Container resolution is deferred to **request time**, not
  baked in at decoration time — this is what allows decoration to always be safe regardless of
  whether `app.state.pywire_container` has been set yet: the container only needs to exist by
  the time the first *request* comes in, not by the time routes are decorated.
- **No ordering limitation** (see the import-order precondition above): because every route is
  rewritten at decoration time regardless of when the container is configured, configuration can
  happen at any point relative to route/router decoration — before, after, or interleaved. If an
  app's container is never configured at all, `Autowired[T]` parameters resolve against the
  module-level default container (the same one `@component` uses), via the `_resolve_autowired`
  fallback above — silently: a forgotten container configuration does not fail, it just resolves
  against a container that may not have the expected component registered. See
  `tests/test_fastapi_integration.py`.
- `pywire_lifespan(app=None, *, container=None, close_on_shutdown=True)` is the sole entry
  point for configuring a container on a FastAPI app: as an ASGI lifespan it writes
  `app.state.pywire_container` at startup and calls `Container.close()` at shutdown, which is
  the only thing in the FastAPI integration that ever fires a bean's `@pre_destroy` /
  `on_close`. Dual-form on the positional argument, exactly like `component` in
  `decorators.py` — a `FastAPI` positional means "I am the lifespan", no positional means "I am
  a factory" — with a `TypeError` for the combination no `@overload` reaches (`app` plus
  configuration) and for a positional that is not a `FastAPI`.
  `close()` runs through `asyncio.to_thread`: it is synchronous and may block on I/O,
  and `anyio` is deliberately not used because it is not a declared dependency of this
  library — it only arrives transitively through starlette. The teardown sits in a
  `finally`, so a nested startup that fails after pywire's own still closes whatever was
  built. Startup raises `RuntimeError` — not a `PyWireError`: no bean is involved and
  `chain`/`requester` would be empty — if `app.state.pywire_container` already holds a
  *different* container (e.g. set directly by application code before the lifespan runs),
  since one of the two configurations would be dead and its beans never closed; the same
  object twice is accepted. The binding is left in place after shutdown, because `close()`
  has no "closed" state and clearing it would silently fall back to a different container.

### Versioning (`scripts/bump-version.sh`)

- `scripts/bump-version.sh <major|minor|patch>` bumps `pyproject.toml`'s `version` via
  `uv version --bump <part>` (which also re-locks `uv.lock`), then commits both files
  as `chore: bump version to X` and creates a local git tag `vX`. It refuses to run on
  a dirty working tree and never pushes — push explicitly with `git push && git push --tags`.
- Lives in `scripts/` at the repo root, not under `src/pywire/`, so it's maintainer-only
  tooling and never ships inside the built package.

### Module layout

`BeanDefinition` is no longer exported from `pywire` — `Container.clear_instances()`
covers what tests used it for.

| File | Responsibility |
|---|---|
| `container.py` | `Container`: registry, register/register_instance/register_factory/resolve/get/clear_instances/close, `_put`, `_Resolution`, `_build_from_class` and `_build_from_factory`, per-subtree rollback with teardown, `_ready_order`, lock; `get_default_container()` and its lazily-created module-level default `Container` |
| `plans.py` | `InjectionPlan.for_class()`: pure inspection of a class's Autowired fields and constructor parameters; `field_label`/`param_label`; rejects unconstructible classes |
| `definitions.py` | `BeanDefinition`: registration metadata, `factory`, `origin`, `teardown`, singleton slot, `ready` flag, cached `InjectionPlan`; `_Origin` |
| `decorators.py` | `@component` |
| `aliases.py` | `service`, `repository`, `agent`, `client`, `provider` — synonyms for `component` |
| `markers.py` | `Autowired[T]`, `evaluate_annotation()`, `callable_hints()`, `resolve_autowired_type()` |
| `exceptions.py` | Immutable `PyWireError` hierarchy with `with_context()` |
| `lifecycle.py` | `pre_destroy` marker decorator; `find_pre_destroy` (pure MRO-respecting discovery); `resolve_teardown` (reconciles `@pre_destroy` with `on_close`) |
| `fastapi.py` | Optional FastAPI integration: `pywire_lifespan()`, `_install_patch`, `_wire_endpoint`, `_resolve_autowired` — resolves bare `Autowired[T]` route parameters via a global `add_api_route` patch, and ties container teardown to the ASGI lifespan |

## Conventions to preserve

- All docstrings, comments, and error/exception messages are written in English only.
- `ruff` config (`pyproject.toml`) enables `E, F, I, UP, RUF, ANN401` rule sets, target `py313`,
  line length 88, first-party import group `pywire`.
