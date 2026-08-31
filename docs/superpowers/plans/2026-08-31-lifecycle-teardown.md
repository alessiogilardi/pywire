# Lifecycle teardown Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a `Container` tear down its beans — `@pre_destroy` methods for classes
you own, `on_close=` for classes you don't — in reverse dependency order, via
`Container.close()` / the context manager protocol, without leaking a resource whose
sibling failed mid-`resolve()`.

**Architecture:** Two independent declaration surfaces normalize into one
`BeanDefinition.teardown: Callable[[Any], None] | None`: `@pre_destroy` (new
`lifecycle.py`, pure MRO-respecting inspection, mirrors `plans.py`'s split from
`Container`) for classes you own, `on_close=` kwarg on `register`/`register_factory`/
`register_instance` for everything else. `Container` tracks `_ready_order` — the order
beans actually reach `ready=True`, which is already a valid topological order by
construction — and `close()` tears down in reverse of it, outside the lock, aggregating
failures into an `ExceptionGroup`. `_roll_back` gets the same treatment for a discarded
`resolve()` subtree, filtered to only the siblings that actually finished
(`ready=True`) — never the bean whose own construction is failing.

**Tech Stack:** Python 3.13, `uv`, pytest, ruff, pyright. No runtime dependencies.
`ExceptionGroup`/`BaseExceptionGroup` are builtins (3.11+), no import needed.

**Spec:** `docs/superpowers/specs/2026-08-31-lifecycle-teardown-design.md` — brainstormed
and grilled (11 questions) before this plan. Read it first; this plan does not
re-explain *why* each decision was made, only *how* to build it.

## Global Constraints

- Python **3.13**; PEP 695 generics (`def register[T](...)`), matching existing code.
- `uv` only. `uv run pytest`, `uv run ruff check .`, `uv run pyright`. Never `pip`.
- All docstrings, comments, and error messages in **English**.
- ruff: `E, F, I, UP, RUF, ANN401`, target `py313`, line length **88**, first-party group
  `pywire`. Verified in-session: `ANN401` does **not** fire on `Any` nested inside
  `Callable[[Any], None]` — only on a bare `Any` as a function's own parameter/return
  annotation. No `# noqa: ANN401` needed anywhere in this plan.
- One class per file; relative imports inside `pywire`, absolute across packages;
  prefer `__init__.py` re-exports over reaching into internal files.
- **Every commit must leave the repo green**: `uv run pytest`, `uv run ruff check .`,
  `uv run pyright` all pass at every one of this plan's commits.
- **No version bump.** `pyproject.toml` stays at `0.5.0`.
- Reuse `RegistrationError` for every new registration-time rejection in this plan —
  no new `PyWireError` subclass. `close()`/`_roll_back` aggregation uses the builtin
  `ExceptionGroup`/`BaseExceptionGroup`, never a pywire-specific type.
- **No `_closed` flag.** `close()` must leave the container reusable — a `resolve()`
  afterward rebuilds cleanly, exactly like after `clear_instances()`.
- **No `atexit` hook** on the default container, and no duck-typing fallback (auto-
  detecting `close()`/`__exit__` on an undeclared bean). A bean without `@pre_destroy`
  or `on_close` simply has no teardown.
- **No runtime type check** tying `on_close`'s expected argument type to what's
  actually resolved — same trust boundary already accepted for `as_type`. This is
  explicitly deferred, not part of this plan.
- Tests never touch `container._registry`, `container._ready_order`, or any other
  private container state — assert through the public API and through side-effecting
  callables (lists appended to), the way `tests/test_registration_apis.py` already does.

---

### Task 1: `lifecycle.py` — `@pre_destroy`, `find_pre_destroy`, `resolve_teardown`

Pure inspection module, no `Container` involved anywhere in this task. Everything here
is testable standalone, exactly like `plans.py`.

**Files:**
- Create: `src/pywire/lifecycle.py`
- Test: `tests/test_lifecycle.py`

**Interfaces:**
- Consumes: `pywire.exceptions.RegistrationError` (already exists).
- Produces:
  - `pre_destroy[F: Callable[..., object]](func: F) -> F` — marker decorator.
  - `find_pre_destroy(cls: type) -> tuple[str, Callable[..., object]] | None` — pure
    MRO-respecting discovery; raises `RegistrationError` for ambiguity, a coroutine
    method, or a bad signature.
  - `resolve_teardown(cls: type, on_close: Callable[[Any], None] | None) -> Callable[[Any], None] | None`
    — the single place `@pre_destroy` discovery and an explicit `on_close` are
    reconciled. Task 2's `register`/`register_factory`/`register_instance` all call
    this on the *declared* type (`register_instance` passes `type(instance)`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_lifecycle.py`:

```python
"""Behavior of @pre_destroy, find_pre_destroy, and resolve_teardown.

Pure inspection: no Container involved. These tests describe what gets
discovered from a class definition alone, and how an explicit on_close
reconciles with it.
"""

import pytest

from pywire import RegistrationError
from pywire.lifecycle import find_pre_destroy, pre_destroy, resolve_teardown


def test_a_class_with_no_marked_method_has_no_teardown():
    class Plain:
        pass

    assert find_pre_destroy(Plain) is None


def test_pre_destroy_returns_the_function_unchanged():
    def close(self) -> None:
        pass

    marked = pre_destroy(close)

    assert marked is close


def test_the_marked_method_is_found_by_name():
    class Resource:
        @pre_destroy
        def shutdown(self) -> None:
            pass

    found = find_pre_destroy(Resource)

    assert found is not None
    name, func = found
    assert name == "shutdown"
    assert func is Resource.__dict__["shutdown"]


def test_a_subclass_inherits_its_base_teardown_method():
    class Base:
        @pre_destroy
        def shutdown(self) -> None:
            pass

    class Derived(Base):
        pass

    found = find_pre_destroy(Derived)

    assert found is not None
    assert found[0] == "shutdown"


def test_overriding_without_redecorating_drops_the_inherited_teardown():
    class Base:
        @pre_destroy
        def shutdown(self) -> None:
            pass

    class Derived(Base):
        def shutdown(self) -> None:  # not re-marked -- opts out
            pass

    assert find_pre_destroy(Derived) is None


def test_overriding_with_redecorating_uses_the_override():
    class Base:
        @pre_destroy
        def shutdown(self) -> None:
            pass

    class Derived(Base):
        @pre_destroy
        def shutdown(self) -> None:
            pass

    name, func = find_pre_destroy(Derived)

    assert name == "shutdown"
    assert func is Derived.__dict__["shutdown"]


def test_two_distinct_marked_methods_are_ambiguous():
    class Broken:
        @pre_destroy
        def shutdown(self) -> None:
            pass

        @pre_destroy
        def close(self) -> None:
            pass

    with pytest.raises(RegistrationError, match="more than one"):
        find_pre_destroy(Broken)


def test_a_coroutine_pre_destroy_method_is_refused():
    class Broken:
        @pre_destroy
        async def shutdown(self) -> None:
            pass

    with pytest.raises(RegistrationError, match="coroutine"):
        find_pre_destroy(Broken)


def test_a_pre_destroy_method_requiring_extra_arguments_is_refused():
    class Broken:
        @pre_destroy
        def shutdown(self, force: bool) -> None:
            pass

    with pytest.raises(RegistrationError, match="force"):
        find_pre_destroy(Broken)


def test_a_pre_destroy_method_with_a_defaulted_extra_argument_is_fine():
    class Fine:
        @pre_destroy
        def shutdown(self, force: bool = False) -> None:
            pass

    assert find_pre_destroy(Fine) is not None


def test_resolve_teardown_returns_none_when_nothing_is_declared():
    class Plain:
        pass

    assert resolve_teardown(Plain, None) is None


def test_resolve_teardown_wraps_the_pre_destroy_method_by_name():
    calls: list[str] = []

    class Resource:
        @pre_destroy
        def shutdown(self) -> None:
            calls.append("closed")

    teardown = resolve_teardown(Resource, None)
    assert teardown is not None

    teardown(Resource())

    assert calls == ["closed"]


def test_resolve_teardown_uses_on_close_when_given():
    calls: list[object] = []

    class Plain:
        pass

    teardown = resolve_teardown(Plain, lambda instance: calls.append(instance))
    assert teardown is not None

    marker = Plain()
    teardown(marker)

    assert calls == [marker]


def test_resolve_teardown_refuses_both_pre_destroy_and_on_close():
    class Resource:
        @pre_destroy
        def shutdown(self) -> None:
            pass

    with pytest.raises(RegistrationError, match="both"):
        resolve_teardown(Resource, lambda instance: None)


def test_resolve_teardown_refuses_a_coroutine_on_close():
    class Plain:
        pass

    async def on_close(instance: object) -> None:
        pass

    with pytest.raises(RegistrationError, match="coroutine"):
        resolve_teardown(Plain, on_close)


def test_resolve_teardown_refuses_an_on_close_with_a_required_extra_argument():
    class Plain:
        pass

    def on_close(instance: object, force: bool) -> None:
        pass

    with pytest.raises(RegistrationError, match="force"):
        resolve_teardown(Plain, on_close)


def test_resolve_teardown_refuses_an_on_close_that_cannot_accept_the_instance():
    class Plain:
        pass

    def on_close() -> None:
        pass

    with pytest.raises(RegistrationError, match="first positional argument"):
        resolve_teardown(Plain, on_close)


def test_resolve_teardown_tolerates_an_uninspectable_on_close(monkeypatch):
    """Best-effort: on_close may be a lambda, a bound method, or a
    C-implemented callable inspect.signature() cannot read. Deterministic via
    monkeypatch rather than hunting for a real uninspectable builtin, whose
    existence is a CPython-version implementation detail."""

    class Plain:
        pass

    def on_close(instance: object) -> None:
        pass

    def raise_type_error(func: object) -> None:
        raise TypeError("no signature found")

    monkeypatch.setattr("pywire.lifecycle.inspect.signature", raise_type_error)

    teardown = resolve_teardown(Plain, on_close)

    assert teardown is on_close
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_lifecycle.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pywire.lifecycle'`.

- [ ] **Step 3: Write `src/pywire/lifecycle.py`**

```python
from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from .exceptions import RegistrationError

_PRE_DESTROY_MARKER = "__pywire_pre_destroy__"

_POSITIONAL_KINDS = (
    inspect.Parameter.POSITIONAL_ONLY,
    inspect.Parameter.POSITIONAL_OR_KEYWORD,
    inspect.Parameter.VAR_POSITIONAL,
)
_VARIADIC_KINDS = (
    inspect.Parameter.VAR_POSITIONAL,
    inspect.Parameter.VAR_KEYWORD,
)


def pre_destroy[F: Callable[..., object]](func: F) -> F:
    """Mark an instance method as this class's teardown hook.

    A pure tag, no wrapping: the function is returned unchanged, and calling
    it directly behaves exactly as if it were undecorated. Container discovers
    it lazily, at registration time, through find_pre_destroy() -- never at
    decoration time, and never by calling the method itself.
    """
    setattr(func, _PRE_DESTROY_MARKER, True)

    return func


def find_pre_destroy(cls: type) -> tuple[str, Callable[..., object]] | None:
    """Return the (name, function) of cls's @pre_destroy method, if any.

    Walks cls.__mro__ most-derived first. For each attribute name, only the
    *first* class in that walk to define it is ever inspected -- a `seen`
    set blocks every later (more base) class from being considered for that
    name at all. That single pass already implements real MRO override
    semantics: a subclass that overrides the method without re-decorating it
    is the first (and only) definition of that name to be inspected, is not
    marked, and the base's marked version is never reached -- opting the
    subclass out. Same rule already documented for Autowired fields on a
    base class in plans.py.

    Raises:
        RegistrationError: more than one distinct method name survives this
            walk (a bean has at most one teardown hook), the surviving
            method is a coroutine function, or its signature requires more
            than the instance argument.
    """
    survivors: list[tuple[str, Callable[..., object]]] = []
    seen: set[str] = set()

    for owner in cls.__mro__:
        for name, value in vars(owner).items():
            if name in seen:
                continue

            seen.add(name)

            if callable(value) and getattr(value, _PRE_DESTROY_MARKER, False):
                survivors.append((name, value))

    if not survivors:
        return None

    if len(survivors) > 1:
        names = ", ".join(name for name, _ in survivors)

        raise RegistrationError(
            f"'{cls.__name__}' has more than one @pre_destroy method: "
            f"{names}. A bean can have at most one teardown hook."
        )

    name, func = survivors[0]
    label = f"{cls.__name__}.{name}"

    if inspect.iscoroutinefunction(func):
        raise RegistrationError(
            f"'{label}' is a coroutine function: it cannot be used as a "
            "teardown hook."
        )

    _reject_bad_teardown_signature(label, func)

    return name, func


def resolve_teardown(
    cls: type, on_close: Callable[[Any], None] | None
) -> Callable[[Any], None] | None:
    """Reconcile @pre_destroy discovery with an explicit on_close kwarg.

    The single place register()/register_factory()/register_instance() call to
    turn a bean's teardown declaration -- whichever of the two sources it
    came from, or neither -- into one Callable[[Any], None] the container
    only ever has to call one way. Called with the *declared* type: cls for
    register/register_factory, type(instance) for register_instance.

    Raises:
        RegistrationError: both a @pre_destroy method and on_close are
            present (no silent precedence rule to remember), on_close is a
            coroutine function, or on_close's signature requires more than
            the instance argument.
    """
    pre_destroy_method = find_pre_destroy(cls)

    if pre_destroy_method is not None and on_close is not None:
        name, _ = pre_destroy_method

        raise RegistrationError(
            f"'{cls.__name__}' has both an on_close callable and a "
            f"@pre_destroy method ('{name}'). Use only one."
        )

    if on_close is not None:
        label = f"on_close for '{cls.__name__}'"

        if inspect.iscoroutinefunction(on_close):
            raise RegistrationError(
                f"{label} is a coroutine function: it cannot be used as a "
                "teardown hook."
            )

        _reject_bad_teardown_signature(label, on_close)

        return on_close

    if pre_destroy_method is None:
        return None

    name, _ = pre_destroy_method

    def call_pre_destroy(instance: object) -> None:
        """Invoke the instance's @pre_destroy method."""
        getattr(instance, name)()

    return call_pre_destroy


def _reject_bad_teardown_signature(label: str, func: Callable[..., object]) -> None:
    """Refuse a teardown callable that cannot be invoked as func(instance).

    Checked from the signature rather than by calling it: a TypeError raised
    *inside* a legitimate teardown call would otherwise be misreported as "bad
    signature". A signature that cannot be introspected at all -- possible for
    on_close, which may be a lambda, a bound method, or a C-implemented
    callable, unlike a @pre_destroy method which is always a plain function --
    is assumed fine and the call is left to speak for itself, mirroring
    plans.py's _reject_unconstructible_new.
    """
    try:
        parameters = list(inspect.signature(func).parameters.values())
    except (TypeError, ValueError):
        return

    if not parameters or parameters[0].kind not in _POSITIONAL_KINDS:
        raise RegistrationError(
            f"Cannot use '{label}' as a teardown hook: it must accept the "
            "torn-down instance as its first positional argument."
        )

    # parameters[0] is where the instance is passed -- `self` for a
    # @pre_destroy method, the sole argument for an on_close callable.
    for parameter in parameters[1:]:
        if parameter.kind in _VARIADIC_KINDS:
            continue

        if parameter.default is inspect.Parameter.empty:
            raise RegistrationError(
                f"Cannot use '{label}' as a teardown hook: parameter "
                f"'{parameter.name}' has no default. A teardown callable is "
                "invoked with the instance as its only argument."
            )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_lifecycle.py -v`
Expected: PASS (18 tests).

- [ ] **Step 5: Lint and type-check**

Run: `uv run ruff check . && uv run pyright`
Expected: both clean.

- [ ] **Step 6: Commit**

```bash
git add src/pywire/lifecycle.py tests/test_lifecycle.py
git commit -m "✨ Add pre_destroy marker and teardown resolution"
```

---

### Task 2: Wire teardown into `BeanDefinition` and the three registration APIs

**Files:**
- Modify: `src/pywire/definitions.py`
- Modify: `src/pywire/container.py:97-198` (`register`, `register_factory`, `register_instance`)
- Modify: `src/pywire/__init__.py`
- Modify: `tests/test_registration_apis.py`
- Modify: `tests/test_components.py`

**Interfaces:**
- Consumes: `lifecycle.resolve_teardown` (Task 1).
- Produces:
  - `BeanDefinition.teardown: Callable[[Any], None] | None = None`.
  - `Container.register[T](self, cls: type[T], *, as_type: type | None = None, on_close: Callable[[T], None] | None = None) -> type[T]`.
  - `Container.register_factory[T](self, target_type: type[T], factory: Callable[[], T], *, on_close: Callable[[T], None] | None = None) -> None`.
  - `Container.register_instance(self, instance: object, *, as_type: type | None = None, on_close: Callable[[object], None] | None = None) -> None`.
  - `pywire.pre_destroy` — now publicly importable.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_registration_apis.py` (this file already imports `Container`,
`RegistrationError`, `DependencyResolutionError` from `pywire`; add `pre_destroy`):

```python
from pywire import pre_destroy  # add to the existing `from pywire import ...` line


def test_register_discovers_a_pre_destroy_method():
    container = Container()
    calls: list[str] = []

    class Resource:
        @pre_destroy
        def shutdown(self) -> None:
            calls.append("closed")

    container.register(Resource)
    container.resolve(Resource)
    container.close()

    assert calls == ["closed"]


def test_register_accepts_on_close_for_a_class_without_pre_destroy():
    container = Container()
    calls: list[object] = []

    class Resource:
        pass

    container.register(Resource, on_close=lambda instance: calls.append(instance))
    instance = container.resolve(Resource)
    container.close()

    assert calls == [instance]


def test_register_refuses_on_close_together_with_pre_destroy():
    container = Container()

    class Resource:
        @pre_destroy
        def shutdown(self) -> None:
            pass

    with pytest.raises(RegistrationError, match="both"):
        container.register(Resource, on_close=lambda instance: None)


def test_register_factory_accepts_on_close():
    container = Container()
    calls: list[object] = []

    container.register_factory(
        Engine,
        lambda: Engine("postgres://"),
        on_close=lambda instance: calls.append(instance),
    )
    instance = container.resolve(Engine)
    container.close()

    assert calls == [instance]


def test_register_instance_accepts_on_close():
    container = Container()
    calls: list[object] = []
    engine = Engine("postgres://")

    container.register_instance(
        engine, on_close=lambda instance: calls.append(instance)
    )
    container.resolve(Engine)
    container.close()

    assert calls == [engine]


def test_register_instance_teardown_conflict_uses_the_runtime_type():
    """find_pre_destroy runs against type(instance), not the as_type key."""
    container = Container()

    class Resource:
        @pre_destroy
        def shutdown(self) -> None:
            pass

    with pytest.raises(RegistrationError, match="both"):
        container.register_instance(
            Resource(), on_close=lambda instance: None
        )
```

Append to `tests/test_components.py` (add `get_default_container` and `pre_destroy`
to its imports if not already present):

```python
def test_component_class_teardown_runs_via_the_default_container():
    """The requirement this whole feature exists for: teardown must work with
    nothing but @component -- no explicit Container.register call anywhere."""
    calls: list[str] = []

    @component
    class Resource:
        @pre_destroy
        def shutdown(self) -> None:
            calls.append("closed")

    get_default_container().resolve(Resource)
    get_default_container().close()

    assert calls == ["closed"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_registration_apis.py tests/test_components.py -v`
Expected: FAIL — `TypeError: register() got an unexpected keyword argument 'on_close'`
(and `ImportError` for `pre_destroy` until Step 5).

- [ ] **Step 3: Add `teardown` to `BeanDefinition`**

In `src/pywire/definitions.py`, add `from typing import Any` to the imports, then add
the field after `plan`:

```python
    plan: InjectionPlan | None = None
    teardown: Callable[[Any], None] | None = None
```

Add to the class docstring's `Attributes:` block:

```
        teardown: Callable invoked with the finished instance during
            Container.close() or a rollback that discards this bean after it
            reached ready=True. None means no teardown was declared. Set by
            lifecycle.resolve_teardown() at registration time, normalizing
            whichever of @pre_destroy / on_close was used -- Container never
            branches on which one it was.
```

- [ ] **Step 4: Thread `on_close` through the three registration methods**

In `src/pywire/container.py`, add the import in alphabetical module order, between
the existing `from .exceptions import (...)` block and `from .plans import ...`:

```python
from .lifecycle import resolve_teardown
```

Replace `register`:

```python
    def register[T](
        self,
        cls: type[T],
        *,
        as_type: type | None = None,
        on_close: Callable[[T], None] | None = None,
    ) -> type[T]:
        """Register a class as a component.

        The class is neither instantiated nor modified. Both happen lazily,
        inside resolve().

        Args:
            cls: The class to construct.
            as_type: Key to register it under, instead of cls itself -- the
                supertype or Protocol its consumers depend on. This **rebinds**:
                afterwards cls is no longer a key of its own. A caller who wants
                both registers twice, and knows they are creating two beans.

                The relation between cls and as_type is **not checked**, here or
                by a type checker: annotating as_type as type[T] would only make
                T widen to the join of the two, which accepts anything.
                issubclass() cannot check a structural Protocol either, and a
                Protocol is the main reason this parameter exists. A wrong
                binding surfaces as an AttributeError on the resolved object.
            on_close: Called with the finished instance during Container.close()
                or a rollback that discards it after completion. Mutually
                exclusive with a @pre_destroy method on cls -- same unchecked
                relation to T as as_type has to cls.

        Raises:
            RegistrationError: as_type is already registered, cls has both a
                @pre_destroy method and on_close, or on_close is unusable as a
                teardown hook (a coroutine function, or a signature that needs
                more than the instance argument).
        """
        teardown = resolve_teardown(cls, on_close)

        self._put(
            as_type if as_type is not None else cls,
            BeanDefinition(cls=cls, teardown=teardown),
        )

        return cls
```

Replace `register_factory`'s signature and body (keep the existing coroutine guard for
`factory` exactly as it is):

```python
    def register_factory[T](
        self,
        target_type: type[T],
        factory: Callable[[], T],
        *,
        on_close: Callable[[T], None] | None = None,
    ) -> None:
        """Register a callable that builds target_type's singleton.

        For objects the container cannot construct itself: third-party types
        that need arguments, and values that must not be built unless something
        actually asks for them.

        The factory runs at most once per container, on first resolution, under
        the same lock that guards every other construction -- so a factory that
        waits on another thread's resolve() deadlocks, exactly as a component
        __init__ would, and a factory that does I/O serializes every resolution
        in this container for its duration.

        Args:
            target_type: The key -- and the type find_pre_destroy() inspects
                for a @pre_destroy method, since the factory itself is opaque
                until called.
            factory: Builds the singleton on first resolution.
            on_close: See register(). Mutually exclusive with a @pre_destroy
                method on target_type.

        Raises:
            RegistrationError: target_type is already registered, factory is a
                coroutine function, target_type has both a @pre_destroy method
                and on_close, or on_close is unusable as a teardown hook.
        """
        if inspect.iscoroutinefunction(factory):
            # Calling it would return a coroutine object, which is not None and
            # so passes every other check -- publishing something that is not an
            # object at all as the bean. Knowingly partial: a callable object
            # whose __call__ is async is not detected.
            raise RegistrationError(
                f"The factory for '{target_type.__name__}' is a coroutine "
                "function: it would publish a coroutine as the bean."
            )

        teardown = resolve_teardown(target_type, on_close)

        self._put(
            target_type,
            BeanDefinition(
                cls=target_type,
                factory=factory,
                origin=_Origin.FACTORY,
                teardown=teardown,
            ),
        )
```

Replace `register_instance`'s signature and body (keep the `None` guard and the named
closure exactly as they are):

```python
    def register_instance(
        self,
        instance: object,
        *,
        as_type: type | None = None,
        on_close: Callable[[object], None] | None = None,
    ) -> None:
        """Register an already-built object as the singleton for its own type.

        For objects the container cannot build: a nested field of a loaded
        configuration, a client constructed at the entry point. The object is
        taken as it is -- **it is not wired**, because the container injects
        only into instances it constructs itself.

        Stored as a factory returning the object it was handed. That is what
        makes teardown uniform: clear_instances() drops the cached instance like
        any other, and rebuilding hands back the same object.

        Args:
            instance: The already-built object to register.
            as_type: Key to register the object under, instead of its runtime
                type. Needed when that type is a generated subclass -- a mock, a
                proxy -- or when consumers should depend on an abstraction. Not
                checked against the object's own type; see register().
            on_close: See register(). find_pre_destroy() inspects
                type(instance) -- the runtime type -- not as_type. Stays lazy
                like every other bean: never resolved means never torn down by
                close(), even though the object already exists.

        Raises:
            RegistrationError: the key is already registered, instance is
                None, type(instance) has both a @pre_destroy method and
                on_close, or on_close is unusable as a teardown hook.
        """
        if instance is None:
            raise RegistrationError("Cannot register None as an instance.")

        target_type = type(instance)
        teardown = resolve_teardown(target_type, on_close)

        def pushed_instance() -> object:
            """Return the object handed to register_instance."""
            return instance

        self._put(
            as_type if as_type is not None else target_type,
            BeanDefinition(
                cls=target_type,
                factory=pushed_instance,
                origin=_Origin.INSTANCE,
                teardown=teardown,
            ),
        )
```

- [ ] **Step 5: Export `pre_destroy`**

In `src/pywire/__init__.py`, add the import in alphabetical module order (between
`.exceptions` and `.markers`):

```python
from .lifecycle import pre_destroy
```

Add `"pre_destroy"` to `__all__`, alphabetically among the lowercase names (between
`"get_default_container"` and `"provider"`).

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_registration_apis.py tests/test_components.py -v`
Expected: FAIL still, on the five new registration/component tests specifically —
they call `container.close()`, which does not exist until Task 3. Confirm the
*old* tests in both files still pass, and that the failures are all
`AttributeError: 'Container' object has no attribute 'close'`. That is the expected,
correct state to stop at for this task.

- [ ] **Step 7: Lint and type-check**

Run: `uv run ruff check . && uv run pyright`
Expected: both clean.

- [ ] **Step 8: Commit**

```bash
git add src/pywire/definitions.py src/pywire/container.py src/pywire/__init__.py tests/test_registration_apis.py tests/test_components.py
git commit -m "✨ Accept on_close and @pre_destroy on all three registration APIs"
```

---

### Task 3: `_ready_order`, `Container.close()`, context manager

**Files:**
- Modify: `src/pywire/container.py` (`__init__`, `_create`, new `close`/`__enter__`/`__exit__`)
- Test: `tests/test_container_close.py`

**Interfaces:**
- Consumes: `BeanDefinition.teardown` (Task 2).
- Produces:
  - `Container._ready_order: list[BeanDefinition]` (private).
  - `Container.close(self) -> None` — may raise `ExceptionGroup`.
  - `Container.__enter__(self) -> Container`, `Container.__exit__(self, *exc_info: object) -> None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_container_close.py`:

```python
"""Behavior of Container.close() and the context manager protocol."""

import threading

import pytest

from pywire import Autowired, Container


def test_close_calls_on_close_for_a_resolved_bean():
    container = Container()
    calls: list[str] = []

    class Resource:
        pass

    container.register(Resource, on_close=lambda instance: calls.append("closed"))
    container.resolve(Resource)

    container.close()

    assert calls == ["closed"]


def test_close_skips_a_bean_that_was_never_resolved():
    container = Container()
    calls: list[str] = []

    class Resource:
        pass

    container.register(Resource, on_close=lambda instance: calls.append("closed"))

    container.close()

    assert calls == []


def test_close_tears_down_in_reverse_ready_order():
    container = Container()
    order: list[str] = []

    class Dep:
        pass

    class Service:
        dep: Autowired[Dep]

    container.register(Dep, on_close=lambda instance: order.append("dep"))
    container.register(Service, on_close=lambda instance: order.append("service"))

    container.resolve(Service)  # builds Dep first, then Service

    container.close()

    assert order == ["service", "dep"]


def test_close_attempts_every_bean_even_if_one_fails():
    container = Container()
    calls: list[str] = []

    class A:
        pass

    class B:
        pass

    def fail(instance: object) -> None:
        raise RuntimeError("boom")

    container.register(A, on_close=fail)
    container.register(B, on_close=lambda instance: calls.append("b"))

    container.resolve(A)
    container.resolve(B)

    with pytest.raises(ExceptionGroup) as exc_info:
        container.close()

    assert calls == ["b"]
    assert len(exc_info.value.exceptions) == 1
    assert isinstance(exc_info.value.exceptions[0], RuntimeError)


def test_close_leaves_the_container_reusable():
    container = Container()

    class Resource:
        pass

    container.register(Resource)
    first = container.resolve(Resource)

    container.close()

    second = container.resolve(Resource)

    assert second is not first


def test_a_second_close_is_a_no_op():
    container = Container()
    calls: list[str] = []

    class Resource:
        pass

    container.register(Resource, on_close=lambda instance: calls.append("closed"))
    container.resolve(Resource)

    container.close()
    container.close()

    assert calls == ["closed"]


def test_context_manager_closes_on_normal_exit():
    calls: list[str] = []

    class Resource:
        pass

    with Container() as container:
        container.register(Resource, on_close=lambda instance: calls.append("closed"))
        container.resolve(Resource)

    assert calls == ["closed"]


def test_context_manager_closes_even_when_the_body_raises():
    calls: list[str] = []

    class Resource:
        pass

    with pytest.raises(RuntimeError, match="boom"):
        with Container() as container:
            container.register(
                Resource, on_close=lambda instance: calls.append("closed")
            )
            container.resolve(Resource)
            raise RuntimeError("boom")

    assert calls == ["closed"]


def test_a_pushed_instance_with_on_close_is_lazy_like_every_other_bean():
    container = Container()
    calls: list[str] = []

    class Resource:
        pass

    container.register_instance(
        Resource(), on_close=lambda instance: calls.append("closed")
    )
    # Deliberately never resolved.

    container.close()

    assert calls == []


def test_close_does_not_block_a_concurrent_resolve_on_another_type():
    """close()'s lock is released before teardown runs, so a slow teardown
    must not stall an unrelated resolve() on another thread."""
    container = Container()
    teardown_started = threading.Event()
    release_teardown = threading.Event()
    fast_resolved: list[object] = []

    class Slow:
        pass

    class Fast:
        pass

    def slow_close(instance: object) -> None:
        teardown_started.set()
        release_teardown.wait(timeout=2)

    container.register(Slow, on_close=slow_close)
    container.register(Fast)
    container.resolve(Slow)

    close_thread = threading.Thread(target=container.close)
    close_thread.start()

    assert teardown_started.wait(timeout=2)

    # Run on its own thread with a bounded join, not called directly here: if
    # close() still held the lock, a direct call would hang this test (and
    # the whole suite) indefinitely instead of failing cleanly.
    resolve_thread = threading.Thread(
        target=lambda: fast_resolved.append(container.resolve(Fast))
    )
    resolve_thread.start()
    resolve_thread.join(timeout=2)

    assert not resolve_thread.is_alive()
    assert len(fast_resolved) == 1
    assert isinstance(fast_resolved[0], Fast)

    release_teardown.set()
    close_thread.join(timeout=2)

    assert not close_thread.is_alive()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_container_close.py -v`
Expected: FAIL — `AttributeError: 'Container' object has no attribute 'close'`.

- [ ] **Step 3: Add `_ready_order` tracking**

In `src/pywire/container.py`, `Container.__init__`, add after `self._current`:

```python
        # Order beans actually reached ready=True. Because a bean only
        # becomes ready after every dependency it needed was itself already
        # resolved, this order is a valid topological order by construction
        # -- close() destroys in reverse of it with no dependency graph
        # needed.
        self._ready_order: list[BeanDefinition] = []
```

In `_create`, at the guarded success point, append alongside setting `ready`:

```python
            if definition.instance is instance:
                definition.ready = True
                self._ready_order.append(definition)
```

- [ ] **Step 4: Add `close()` and the context manager protocol**

Add after `clear_instances`:

```python
    def close(self) -> None:
        """Tear down every resolved bean's teardown hook, then reset.

        Destroys in reverse-ready order -- dependents before their
        dependencies, the same guarantee Spring gets from an explicit
        dependency map, here free from _ready_order's construction. Every
        bean is attempted regardless of earlier failures; failures are
        aggregated and raised together, never logged-and-swallowed.

        The lock is held only to snapshot which (definition, instance) pairs
        need tearing down and to reset container state -- never across the
        teardown calls themselves, which may do slow I/O (closing a pool) and
        must not block an unrelated resolve() on another thread. Nothing
        touches self._ready_order after it is captured here, so a teardown
        that reentrantly calls resolve() only ever appends to a fresh, already
        -cleared list.

        Leaves the registry intact, exactly like clear_instances() -- there is
        no "closed" state. A resolve() afterward simply rebuilds. A second
        close() is a safe no-op: _ready_order is already empty.

        Raises:
            ExceptionGroup: one or more teardown callables raised. Every
                other bean was still attempted.
        """
        with self._lock:
            pending: list[tuple[Callable[[Any], None], object]] = []

            for definition in reversed(self._ready_order):
                teardown = definition.teardown
                instance = definition.instance

                if teardown is not None and instance is not None:
                    pending.append((teardown, instance))

            self._ready_order.clear()
            self.clear_instances()

        errors: list[Exception] = []

        for teardown, instance in pending:
            try:
                teardown(instance)
            except Exception as exc:
                # Never BaseException: a KeyboardInterrupt mid-teardown must
                # interrupt close() immediately, the same way it would
                # anywhere else, rather than being trapped alongside ordinary
                # teardown failures.
                errors.append(exc)

        if errors:
            raise ExceptionGroup("errors during Container.close()", errors)

    def __enter__(self) -> Container:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
```

`teardown` and `instance` are read into locals before the `if`, exactly the way
`register_factory`'s `factory` local narrows `Callable[[], object] | None` for
`_build_from_factory` elsewhere in this file: pyright does not carry narrowing across
statements for an attribute expression (`definition.teardown`) the way it does for a
local variable, so reading into a local first is what avoids both a redundant re-read
and a type-checker complaint -- no `assert` needed. `container.py` already has
`from typing import cast`; extend it to `from typing import Any, cast` (used here and
in the `_roll_back` rewrite in Task 4).

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_container_close.py -v`
Expected: PASS (10 tests).

- [ ] **Step 6: Full suite, lint, types**

Run: `uv run pytest && uv run ruff check . && uv run pyright`
Expected: all green, including the Task 2 tests that were waiting on `close()`.

- [ ] **Step 7: Commit**

```bash
git add src/pywire/container.py tests/test_container_close.py
git commit -m "✨ Add Container.close() and the context manager protocol"
```

---

### Task 4: Rollback teardown for a discarded subtree

**Files:**
- Modify: `src/pywire/container.py` (`_create`'s except clause, `_roll_back`)
- Modify: `tests/test_container_semantics.py`

**Interfaces:**
- Consumes: `BeanDefinition.teardown`, the `_create`/`_roll_back` pair (existing).
- Produces: `Container._roll_back(self, resolution: _Resolution, created_mark: int) -> list[Exception]`
  — same method, new return type. Its only caller is `_create`; no other code in the
  repo calls it.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_container_semantics.py` (already imports `Autowired`,
`Container`, `pytest`, and this repo's classes like `Dep`/`Service` at the top of the
file — reuse `Dep` where it fits, otherwise define locally as shown):

```python
def test_rollback_never_calls_teardown_on_the_bean_that_is_failing():
    container = Container()
    closed: list[str] = []

    class Failing:
        def __init__(self) -> None:
            raise RuntimeError("boom")

    container.register(Failing, on_close=lambda instance: closed.append("failing"))

    with pytest.raises(RuntimeError, match="boom"):
        container.resolve(Failing)

    assert closed == []


def test_rollback_tears_down_a_completed_sibling_but_not_the_failing_bean():
    container = Container()
    closed: list[str] = []

    class RollbackDep:
        pass

    class RollbackFailing:
        def __init__(self) -> None:
            raise RuntimeError("boom")

    class RollbackHost:
        dep: Autowired[RollbackDep]
        failing: Autowired[RollbackFailing]

    container.register(
        RollbackDep, on_close=lambda instance: closed.append("dep")
    )
    container.register(RollbackFailing)
    container.register(RollbackHost)

    with pytest.raises(RuntimeError, match="boom"):
        container.resolve(RollbackHost)

    assert closed == ["dep"]


def test_rollback_combines_original_and_teardown_failures_into_an_exception_group():
    container = Container()

    class RollbackDep2:
        pass

    class RollbackFailing2:
        def __init__(self) -> None:
            raise RuntimeError("boom")

    class RollbackHost2:
        dep: Autowired[RollbackDep2]
        failing: Autowired[RollbackFailing2]

    def fail_close(instance: object) -> None:
        raise ValueError("teardown also failed")

    container.register(RollbackDep2, on_close=fail_close)
    container.register(RollbackFailing2)
    container.register(RollbackHost2)

    with pytest.raises(ExceptionGroup) as exc_info:
        container.resolve(RollbackHost2)

    exceptions = exc_info.value.exceptions
    assert any(isinstance(exc, RuntimeError) for exc in exceptions)
    assert any(isinstance(exc, ValueError) for exc in exceptions)


def test_rollback_uses_base_exception_group_for_a_non_exception_original_failure():
    container = Container()

    class RollbackDep3:
        pass

    class RollbackFailing3:
        def __init__(self) -> None:
            raise KeyboardInterrupt

    class RollbackHost3:
        dep: Autowired[RollbackDep3]
        failing: Autowired[RollbackFailing3]

    def fail_close(instance: object) -> None:
        raise ValueError("teardown also failed")

    container.register(RollbackDep3, on_close=fail_close)
    container.register(RollbackFailing3)
    container.register(RollbackHost3)

    with pytest.raises(BaseExceptionGroup) as exc_info:
        container.resolve(RollbackHost3)

    exceptions = exc_info.value.exceptions
    assert any(isinstance(exc, KeyboardInterrupt) for exc in exceptions)
    assert any(isinstance(exc, ValueError) for exc in exceptions)
```

Field resolution order matters for the last three tests: `RollbackDep*` is declared
before `RollbackFailing*` on the host class, and `plans.py`'s `_plan_fields` preserves
declaration order, so `Dep*` always finishes (reaching `ready=True`) before
`Failing*` raises.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_container_semantics.py -v`
Expected: FAIL — the two `KeyboardInterrupt`/`fail_close` tests currently see a plain
`RuntimeError`/`KeyboardInterrupt` (no combination happens yet), and
`test_rollback_tears_down_a_completed_sibling_but_not_the_failing_bean` sees
`closed == []` instead of `["dep"]`. `test_rollback_never_calls_teardown_on_the_bean_that_is_failing`
already passes — nothing calls `on_close` at all yet, which happens to satisfy
`closed == []`; that is a coincidence of the current no-op state, not evidence the
behavior is implemented. Do not skip Steps 3-4 on the strength of it.

- [ ] **Step 3: Rewrite `_roll_back`**

Replace the whole method in `src/pywire/container.py`:

```python
    def _roll_back(
        self, resolution: _Resolution, created_mark: int
    ) -> list[Exception]:
        """Discard every instance built inside the failing subtree.

        Clearing only the failing bean is not enough: a partner already
        initialized during the same call may hold a reference to a half-built
        object, which would leave the registry handing out an instance the
        container has otherwise disowned.

        Rolling back per *subtree* rather than only at the outermost frame
        matters for a reachable case: a component whose __init__ resolves an
        optional dependency inside a try/except swallows the failure, so it
        never reaches an outer frame at all.

        `ready` is cleared alongside `instance` -- leaving it set would keep a
        rolled-back bean visible to the unsynchronised fast path in resolve()
        forever, which is worse than the problem `ready` solves -- and it is
        cleared *first*, because that fast path reads `ready` before `instance`:
        undoing the writes in the opposite order narrows, but does not close,
        the window in which an interleaved reader could observe a disowned
        instance as ready, since the two stores remain independent and
        non-atomic. `plan` is deliberately preserved: it is a pure function of
        the class, and a construction failure says nothing about its validity.

        Teardown is attempted for every discarded definition that reached
        `ready=True` before the failure -- a genuinely complete sibling built
        earlier in the same subtree. The bean whose own construction is
        failing right now is *never* torn down: its instance was published
        early (before __init__ ran) but it never reached ready=True, so
        calling a teardown hook on it would run cleanup logic against a
        half-initialized object. `ready` is read here, before the loop below
        clears it -- that read is what makes the distinction possible at all.
        Teardown order mirrors close(): reverse of construction.

        Returns:
            Every exception a teardown callable raised, so the caller can
            combine them with the exception that triggered the rollback
            rather than losing one or the other.
        """
        to_discard = resolution.created[created_mark:]
        completed: list[tuple[Callable[[Any], None], object]] = []

        for definition in reversed(to_discard):
            teardown = definition.teardown
            instance = definition.instance

            if definition.ready and teardown is not None and instance is not None:
                completed.append((teardown, instance))

        for definition in to_discard:
            definition.ready = False
            definition.instance = None

        del resolution.created[created_mark:]

        errors: list[Exception] = []

        for teardown, instance in completed:
            try:
                teardown(instance)
            except Exception as exc:
                errors.append(exc)

        return errors
```

Same local-variable-first idiom as `close()` in Task 3, for the same pyright reason.

- [ ] **Step 4: Combine the two exceptions in `_create`**

Replace `_create`'s except clause:

```python
        except BaseException as exc:
            teardown_errors = self._roll_back(resolution, created_mark)

            if teardown_errors:
                # exc may be a bare BaseException (KeyboardInterrupt,
                # SystemExit) -- ExceptionGroup accepts only Exception
                # members, so the group type is picked to match what exc
                # actually is.
                group_type = (
                    ExceptionGroup
                    if isinstance(exc, Exception)
                    else BaseExceptionGroup
                )

                raise group_type(
                    f"'{target_type.__name__}' failed to construct, and "
                    "rollback teardown also raised",
                    [exc, *teardown_errors],
                ) from exc

            raise
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_container_semantics.py -v`
Expected: PASS, including all four new tests and every pre-existing rollback test
(`test_failed_resolution_leaves_no_partial_instance_behind`,
`test_caught_inner_failure_leaves_no_partial_instance_behind`,
`test_rollback_clears_ready_so_a_later_success_is_not_masked`) — none of those
register a teardown, so `completed` is always empty for them and `_roll_back`'s return
value is always `[]`, an unchanged `raise` at the end of `_create`.

- [ ] **Step 6: Full suite, lint, types**

Run: `uv run pytest && uv run ruff check . && uv run pyright`
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add src/pywire/container.py tests/test_container_semantics.py
git commit -m "🐛 Tear down completed siblings when a resolve() subtree rolls back"
```

---

### Task 5: Documentation

**Files:**
- Modify: `README.md` (new section)
- Modify: `CLAUDE.md` (module table, new architecture subsection)

**Interfaces:**
- Consumes: everything above. Produces no code.

- [ ] **Step 1: Add a section to `README.md`**

Insert a new `## Tearing beans down` section immediately before `## FastAPI
Integration` (matching the existing heading level and style of the "Objects the
container cannot build" section above it):

````markdown
## Tearing beans down

A bean that owns an external resource -- a connection pool, a file handle -- needs
to release it when the container's work is done. Two ways to declare how, matching
the two ways a bean gets built:

```python
from pywire import Container, pre_destroy

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
````

- [ ] **Step 2: Update `CLAUDE.md`**

In the **module layout** table, add a row for `lifecycle.py` (alphabetically, between
`fastapi.py` and `markers.py` if the table is alphabetized, otherwise immediately
after `decorators.py`/`aliases.py` — match whatever order the existing table already
uses) and extend the `container.py` and `definitions.py` rows:

```markdown
| `container.py` | `Container`: registry, register/register_instance/register_factory/resolve/get/clear_instances/close, `_put`, `_Resolution`, `_build_from_class` and `_build_from_factory`, per-subtree rollback with teardown, `_ready_order`, lock |
| `definitions.py` | `BeanDefinition`: registration metadata, `factory`, `origin`, `teardown`, singleton slot, `ready` flag, cached `InjectionPlan`; `_Origin` |
| `lifecycle.py` | `pre_destroy` marker decorator; `find_pre_destroy` (pure MRO-respecting discovery); `resolve_teardown` (reconciles `@pre_destroy` with `on_close`) |
```

Add a new subsection after **Component decorators (`decorators.py`, `aliases.py`)**
and before **FastAPI integration (`fastapi.py`)**:

```markdown
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
rather than handed a teardown hook expecting a fully-built object. `_create`'s except
clause combines -- never replaces -- the exception that triggered the rollback with
whatever `_roll_back`'s own teardown attempts raised, picking `ExceptionGroup` or
`BaseExceptionGroup` by whether the triggering exception is itself an `Exception`:
`_roll_back` runs even for a bare `BaseException` like `KeyboardInterrupt`, which
`ExceptionGroup` cannot hold.

No `atexit` hook and no duck-typing fallback (auto-detecting `close()`/`__exit__` on
an undeclared bean): a bean without either declaration simply has no teardown. No
runtime check ties `on_close`'s declared argument type to what actually gets resolved
-- the same trust boundary already accepted for `as_type`, `Callable[[Any], None]`
being the honest annotation once stored on `BeanDefinition`, same reasoning as
`as_type: type | None` being bare instead of `type[T]`.
```

- [ ] **Step 3: Verify the docs describe the code that exists**

Run: `uv run pytest && uv run ruff check . && uv run pyright`
Expected: green. Re-read the README section and the new CLAUDE.md subsection against
`src/pywire/container.py` and `src/pywire/lifecycle.py` — every method name, keyword,
and error message referenced must exist exactly as written.

- [ ] **Step 4: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "📝 Document lifecycle teardown"
```

---

## Done criteria

- `uv run pytest`, `uv run ruff check .`, `uv run pyright` all pass.
- Each of the five commits, checked out on its own, has a green `uv run pytest`.
- `grep -rn "_ready_order\|_registry" tests/` returns nothing — no test reaches into
  container internals.
- `from pywire import pre_destroy` works; `from pywire.lifecycle import
  find_pre_destroy, resolve_teardown` works (internal, not re-exported from
  `pywire/__init__.py`).
- `pyproject.toml` still says `0.5.0`.
- Every `RegistrationError` raised by this plan's code names the class (and method,
  where relevant) it is refusing — no generic "invalid teardown" message.

## Follow-up (not this plan)

- **Runtime type-safety check for `on_close`** (and, bundled with it, `as_type`) —
  explicitly deferred in the design spec as a broader enhancement, not a one-off fix
  here.
- **`@configuration`/`@Bean`-style factory methods, qualifiers, `PROTOTYPE` scope** —
  unrelated YAGNI already deferred by the previous plan; unaffected by this one.
