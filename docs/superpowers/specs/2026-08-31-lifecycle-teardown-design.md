# Lifecycle teardown (`@pre_destroy`, `on_close`, `Container.close()`)

Status: **discussed, brainstormed, and grilled in chat — implementation not started**.
Written down to resume later — see final section for what's still open.

## Motivation

`Container` currently has no notion of teardown: `clear_instances()` drops cached
singletons but calls nothing on them. Any bean that owns an external resource (a DB
connection pool, a file handle) leaks it once the container that built it goes out of
scope. This mirrors Spring's `DisposableBean` / `@PreDestroy` / `@Bean(destroyMethod=...)`
trio, adapted to pywire's constraints: no runtime dependencies, no implicit duck-typing
(`register_instance`'s existing rule — "the object is taken as it is" — sets the bar),
and it must work for beans registered with nothing but `@component`.

## Declaration mechanism

Two ways to declare teardown for a bean, chosen to map onto pywire's two existing
registration surfaces (decorator for classes you own, `Container.register*` for
everything else) rather than reproducing Spring's three:

- **`@pre_destroy`** (new `lifecycle.py`, sibling of `markers.py`): a pure marker
  decorator for an instance method inside a class you own. `pre_destroy(func)` sets
  `func.__pywire_pre_destroy__ = True` and returns `func` unchanged — no wrapping, no
  runtime behavior, exactly the same shape as `Autowired` being pure annotation.
  Works with `@component`, `Container.register()`, or a class instantiated inside a
  factory — it's orthogonal to *how* the class gets registered.
- **`on_close=`** kwarg on `register()`, `register_factory()`, `register_instance()`
  (all kwarg-only): for third-party classes that can't be decorated. Same shape as
  Spring's `destroyMethod`.

`find_pre_destroy(cls) -> tuple[str, Callable] | None` (also `lifecycle.py`) is pure
inspection, no container involved: walks `cls.__mro__` most-derived first, collects
names marked in each class's own `__dict__`, then resolves each candidate name via
`getattr(cls, name)` to respect real MRO override — a subclass that overrides the
method without re-decorating it "wins" and the base's marked method no longer counts.
Mirrors the existing documented behavior for `Autowired` fields on a base class. More
than one distinct marked name surviving resolution is an ambiguity error.

**Discovery happens at registration time**, not at first `resolve()` like
`InjectionPlan` — unlike Autowired planning this needs no `get_type_hints()`, so there's
no forward-reference reason to defer it, and registration-time is what lets conflicts
be reported immediately (see below) instead of surfacing lazily.

For `register`/`register_factory` the type inspected is the *declared* type (`cls` /
`target_type`), matching how `BeanDefinition.cls` already stores the declared type on
the factory path, not whatever the factory happens to return. For `register_instance`
it's `type(instance)`, the runtime type — the only type available at that call site.

**`register_instance` stays lazy.** A pushed instance only enters `_ready_order` (and
becomes teardown-eligible) after the first `resolve()` of its type — same rule as every
other bean, unchanged by this feature. An `on_close=` registered but never resolved is
simply never closed. Considered and rejected: making `register_instance` eagerly ready
at registration time, since the object already exists — rejected because it would
change existing, already-relied-upon laziness for *every* `register_instance` caller,
not just the ones using `on_close`, which is a bigger blast radius than this feature
needs. Must be documented clearly so it isn't a surprise.

**Validation, all eager at registration time, all in `lifecycle.py` as pure functions**
(mirroring `plans.py`'s split from `Container._plan()` — no resolution context to
attach here, since none of this runs inside a `resolve()`; `container.py` just
translates a validation failure into `RegistrationError`):
- Both a `@pre_destroy` method *and* `on_close=` present for the same bean →
  `RegistrationError`. No silent precedence rule to remember — same posture as
  `component()` rejecting `cls` + `as_type` together.
- Either source resolving to a coroutine function → `RegistrationError`, mirroring the
  existing check in `register_factory` for `factory` itself.
- **`@pre_destroy` method signature**: must accept zero arguments beyond `self` (no
  required parameter without a default) → `RegistrationError` naming the class and
  method if violated. Same "fail fast, explicit" principle already applied to
  constructors by `InjectionPlan.for_class()` — catches a broken teardown method at
  registration instead of as a bare `TypeError` buried inside a `close()`-time
  `ExceptionGroup`. The method's shape is always predictable (a plain function defined
  in the class body), so `inspect.signature()` is reliable here.
- **`on_close` signature**: same check, but **best-effort**. Unlike a `@pre_destroy`
  method, `on_close` can be a lambda, a bound method, a callable object, even a
  C-implemented builtin — `inspect.signature()` can fail or be unreliable on some of
  these. If it succeeds and shows a required parameter beyond the first, reject eagerly
  exactly like the `@pre_destroy` case; if introspection itself fails, let it through
  unchecked and let a bad call fail later inside `close()`'s `ExceptionGroup`, same as
  today's untyped trust boundary around `factory` and `as_type`.

Both sources normalize into one `Callable[[object], None]` before being stored, so
downstream code (`close()`, `_roll_back`) only ever deals with one shape. For a
`@pre_destroy`-sourced teardown the wrapper is `lambda instance: getattr(instance, name)()`.

**Typing of `on_close` is deliberately unchecked at runtime.** `Callable[[T], None]` at
each call site narrows for the caller/type-checker, but once stored on
`BeanDefinition.teardown` it's untyped `Callable[[object], None]` — no `isinstance`
check ties the callable's expected argument type back to what's actually resolved.
Exactly the same trust boundary already accepted for `as_type` (`type | None`, checked
by nothing, documented as such). A wrong binding surfaces as an exception raised from
inside the callable itself, not from the container.
→ **Future enhancement, explicitly out of scope here**: add runtime type-safety checks
across `as_type`, `on_close`, and any other currently-unchecked binding, as one unified
pass rather than a one-off fix just for this feature.

## `BeanDefinition` and ordering

One new field: `teardown: Callable[[object], None] | None = None`.

`Container.__init__` gains `self._ready_order: list[BeanDefinition] = []`. In `_create`
(`container.py:368-369`), at the same guarded point that currently sets
`definition.ready = True` — same lock, same "instance wasn't disowned mid-build"
condition — also append the definition to `_ready_order`.

No dependency graph is needed: because a bean only reaches `ready=True` after every
field/constructor dependency it needed has itself already been resolved, the order
beans become ready *is* a valid topological order by construction. Destroying in
reverse of that order gives the same "dependents die before their dependencies"
guarantee Spring gets from its explicit `dependentBeanMap`, for free.

A bean that's registered but never resolved never enters `_ready_order` and is
correctly invisible to `close()` — no special-casing required.

## `Container.close()`, context manager, rollback integration

```python
def close(self) -> None:
    with self._lock:
        pending = [
            (definition, definition.instance)
            for definition in reversed(self._ready_order)
            if definition.teardown is not None and definition.instance is not None
        ]
        self._ready_order.clear()
        self.clear_instances()

    # Deliberately outside the lock: a slow teardown (pool shutdown doing I/O)
    # must not block an unrelated resolve() on another thread. The pending list
    # was captured above, under the lock, so nothing here touches live container
    # state -- there is no self._ready_order left to mutate out from under an
    # iterator, which is also what makes a reentrant resolve() triggered by a
    # teardown safe: it only ever appends to a *fresh*, already-cleared list.
    errors: list[Exception] = []
    for definition, instance in pending:
        try:
            definition.teardown(instance)
        except Exception as exc:
            errors.append(exc)

    if errors:
        raise ExceptionGroup("errors during Container.close()", errors)

def __enter__(self) -> Container:
    return self

def __exit__(self, *exc_info: object) -> None:
    self.close()
```

Every bean's teardown is attempted regardless of earlier failures; failures are
aggregated and raised together as an `ExceptionGroup`, never logged-and-swallowed —
consistent with the project-wide "fail explicitly, never swallow exceptions" rule, and
avoids pulling in a logging dependency pywire has never needed. `close()`'s loop catches
`Exception` only, deliberately never `BaseException`: a `KeyboardInterrupt` mid-teardown
must interrupt immediately, the same way it would anywhere else in the process, rather
than being trapped into the aggregate alongside ordinary teardown failures.

`close()` leaves the registry intact (same as `clear_instances()` today), so the
container stays reusable afterward — **there is no "closed" state**. A `resolve()`
called after `close()` simply rebuilds from scratch, exactly like after
`clear_instances()`. Considered and rejected: a `_closed` flag rejecting further
`resolve()` calls with a dedicated error, mirroring how a closed file/socket refuses
further use — rejected because `close()` is a superset of the already-established
"reset" semantics of `clear_instances()`, and no other container state is ever marked
permanently invalid. This must be documented explicitly, since it deviates from the
closed-resource intuition Python users otherwise have. A second `close()` call is a safe
no-op: `_ready_order` is already empty by the time it runs.

**Rollback leak closed, precisely.** `_roll_back` (`container.py:473-500`) currently
discards a failed subtree without calling anything, which would leak any resource
already opened by a sibling bean that succeeded before a later sibling failed in the
same `resolve()`. The subtlety: `resolution.created[created_mark:]` always includes the
bean whose own construction is what's failing right now — its instance was published
early (before `__init__` ran) but it never reached `ready=True`, so calling teardown on
it would run cleanup logic against a half-initialized object. **Only definitions that
had actually reached `ready=True` before the failure are torn down**; the failing bean
itself, and anything else that never finished, is skipped. Order is reverse of
construction, same as `close()`.

```python
def _roll_back(
    self, resolution: _Resolution, created_mark: int
) -> list[Exception]:
    to_discard = resolution.created[created_mark:]
    # Captured before the loop below clears `ready` -- this is the one read that
    # decides whether a definition is eligible for teardown at all.
    completed = [d for d in reversed(to_discard) if d.ready and d.teardown is not None]

    for definition in to_discard:
        definition.ready = False
        definition.instance = None

    del resolution.created[created_mark:]

    errors: list[Exception] = []
    for definition in completed:
        try:
            definition.teardown(definition.instance)
        except Exception as exc:
            errors.append(exc)

    return errors
```

`_create`'s except clause combines — never replaces — the original exception with any
rollback-teardown failures. Because this branch catches `BaseException` (rollback must
run even for a `KeyboardInterrupt`, unlike `close()`'s own loop), the original exception
being combined might not itself be an `Exception` — and `ExceptionGroup` only accepts
`Exception` members. The group type is picked accordingly:

```python
except BaseException as exc:
    teardown_errors = self._roll_back(resolution, created_mark)
    if teardown_errors:
        group_type = ExceptionGroup if isinstance(exc, Exception) else BaseExceptionGroup
        raise group_type(
            f"'{target_type.__name__}' failed to construct, and rollback "
            "teardown also raised", [exc, *teardown_errors],
        ) from exc
    raise
```

## Explicitly out of scope

- No `atexit` hook or other implicit trigger on the default container. Consistent with
  "explicit behavior, no hidden side effects" — closing the default container is
  `get_default_container().close()`, called by hand.
- No duck-typing fallback (e.g. auto-detecting `close()`/`__exit__` on a bean that
  declares neither `@pre_destroy` nor `on_close`, à la Spring's inferred
  `destroyMethod`). A bean without an explicit declaration simply has no teardown.
- No `_closed` flag / no rejection of `resolve()` after `close()` — see above.
- No runtime type-safety check tying `on_close`'s expected argument type to what's
  actually resolved — see the `on_close` typing note above; tracked as a future,
  broader enhancement alongside `as_type`.

## Module layout addition

| File | Responsibility |
|---|---|
| `lifecycle.py` | `pre_destroy(func)` marker decorator; `find_pre_destroy(cls)` pure MRO-respecting inspection; eager signature validation for both `@pre_destroy` methods and `on_close` callables (best-effort for the latter) |

`container.py`, `definitions.py`, `decorators.py`, `aliases.py` all need touching
(new kwarg threaded through `register`/`register_factory`/`register_instance`); the
`aliases.py` synonyms (`service`, `repository`, etc.) need no change since they're pure
aliases of `component`, which itself doesn't take `on_close` (only `@pre_destroy`
reaches decorator-registered classes).

## Open items when resuming

- No implementation started — this is design only.
- Exact wording of the new `RegistrationError` messages (dual-teardown conflict,
  ambiguous `@pre_destroy`, coroutine rejection on both sources, bad-signature
  rejection on both sources) not yet drafted.
- Test plan not drafted (TDD applies per project convention): needs coverage for
  `@pre_destroy` alone, `on_close` alone, both-present conflict, coroutine rejection on
  both paths, bad-signature rejection on both paths (including the best-effort
  fallback-to-unchecked case for an uninspectable `on_close`), MRO override shadowing a
  base's `@pre_destroy`, ambiguous multi-method case, reverse-ready-order teardown
  sequencing, `_roll_back` teardown limited to `ready=True` siblings only (never the
  failing bean itself), `ExceptionGroup`/`BaseExceptionGroup` selection in both `close()`
  and rollback, `close()`'s lock released before the teardown loop (verify a slow
  teardown doesn't block a concurrent unrelated `resolve()`), double `close()`,
  `resolve()` after `close()` rebuilding cleanly, context-manager usage, `register_instance`
  laziness (`on_close` set but never resolved → never closed), ordinary bean with no
  teardown declared (no-op path).
- Next step when resuming: `superpowers:writing-plans` to turn this into an
  implementation plan (TDD, one file at a time per project's package-layout rules) —
  do not skip straight to code.
