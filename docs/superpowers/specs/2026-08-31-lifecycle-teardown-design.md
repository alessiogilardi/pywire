# Lifecycle teardown (`@pre_destroy`, `on_close`, `Container.close()`)

Status: **discussed and approved in chat, implementation not started**. Written down
to resume later — see final section for what's still open.

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

**Conflict and validation rules, checked eagerly at registration:**
- Both a `@pre_destroy` method *and* `on_close=` present for the same bean →
  `RegistrationError`. No silent precedence rule to remember — same posture as
  `component()` rejecting `cls` + `as_type` together.
- Either source resolving to a coroutine function → `RegistrationError`, mirroring the
  existing check in `register_factory` for `factory` itself.

Both sources normalize into one `Callable[[object], None]` before being stored, so
downstream code (`close()`, `_roll_back`) only ever deals with one shape. For a
`@pre_destroy`-sourced teardown the wrapper is `lambda instance: getattr(instance, name)()`.

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
        errors: list[Exception] = []
        for definition in reversed(self._ready_order):
            if definition.teardown is not None and definition.instance is not None:
                try:
                    definition.teardown(definition.instance)
                except Exception as exc:
                    errors.append(exc)
        self._ready_order.clear()
        self.clear_instances()
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
avoids pulling in a logging dependency pywire has never needed. `close()` leaves the
registry intact (same as `clear_instances()` today), so the container is safely
reusable afterward, and a second `close()` is a no-op because `_ready_order` is already
empty.

**Rollback leak closed.** `_roll_back` (`container.py:473-500`) currently discards a
failed subtree without calling anything, which would leak any resource already opened
by a sibling bean that succeeded before a later sibling failed in the same `resolve()`.
Fixed by having `_roll_back` attempt teardown (in reverse construction order) for every
definition in the discarded subtree, collecting failures rather than raising
immediately. The caller (`_create`'s except block) combines — never replaces — the
original exception with any rollback-teardown failures:

```python
except BaseException as exc:
    teardown_errors = self._roll_back(resolution, created_mark)
    if teardown_errors:
        raise ExceptionGroup(
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

## Module layout addition

| File | Responsibility |
|---|---|
| `lifecycle.py` | `pre_destroy(func)` marker decorator; `find_pre_destroy(cls)` pure MRO-respecting inspection |

`container.py`, `definitions.py`, `decorators.py`, `aliases.py` all need touching
(new kwarg threaded through `register`/`register_factory`/`register_instance`); the
`aliases.py` synonyms (`service`, `repository`, etc.) need no change since they're pure
aliases of `component`, which itself doesn't take `on_close` (only `@pre_destroy`
reaches decorator-registered classes).

## Open items when resuming

- No implementation started — this is design only.
- Exact wording of the new `RegistrationError` messages (dual-teardown conflict,
  ambiguous `@pre_destroy`, coroutine rejection for both sources) not yet drafted.
- Whether `find_pre_destroy`'s ambiguity/ coroutine checks belong in `lifecycle.py`
  itself (pure, container-agnostic, testable standalone — matches how `plans.py` stays
  pure and lets `Container._plan` add context) or inline in `container.py`'s
  registration methods. Leaning `lifecycle.py`, matching the `plans.py` precedent, but
  not decided.
- Test plan not drafted (TDD applies per project convention): needs coverage for
  `@pre_destroy` alone, `on_close` alone, both-present conflict, coroutine rejection on
  both paths, MRO override shadowing a base's `@pre_destroy`, ambiguous multi-method
  case, reverse-ready-order teardown sequencing, `_roll_back` teardown-on-discarded-
  subtree, `ExceptionGroup` aggregation in both `close()` and rollback, double `close()`,
  context-manager usage, ordinary bean with no teardown declared (no-op path).
- Not yet decided: does `on_close`'s type signature stay `Callable[[T], None]` per call
  site (as sketched in chat) even though `BeanDefinition.teardown` is stored untyped as
  `Callable[[object], None]` — i.e. where does the narrowing happen, is it purely a
  call-site generic on `register`/`register_factory`/`register_instance` with no runtime
  check? (Same pattern already exists for `as_type` — checked by nothing, documented as
  such — so likely the same answer here, but not explicitly confirmed with the user.)
- Next step when resuming: `superpowers:writing-plans` to turn this into an
  implementation plan (TDD, one file at a time per project's package-layout rules) —
  do not skip straight to code.
