# Registration APIs and supertype binding — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the composition root push objects into a `Container`
(`register_instance`, `register_factory`) and let any registration choose its own
key (`as_type=`), without weakening a single invariant established by spec 1.

**Architecture:** One new field, `BeanDefinition.factory`, decides how a bean is
obtained: `None` keeps today's path (plan → `__new__` → early publication → field
injection → `__init__`), non-`None` calls the factory and publishes what it returns.
`register_instance(obj)` is sugar over the factory path — a **named** closure
returning the pushed object — which is what keeps `clear_instances()` and
`_roll_back()` branch-free. `as_type=` rebinds the registry key; it never adds a
second one.

**Tech Stack:** Python 3.13, `uv`, pytest, ruff, pyright. No runtime dependencies.
FastAPI only in the optional extra.

**Spec:** `docs/superpowers/specs/2026-08-20-registration-apis-and-supertype-binding-design.md`

## Global Constraints

- Python **3.13**; `type[T]` generics use PEP 695 syntax (`def register[T](...)`),
  matching the existing code.
- `uv` only. Run tests with `uv run pytest`, lint with `uv run ruff check .`,
  type-check with `uv run pyright`. Never `pip`.
- All docstrings, comments, and error messages in **English**.
- ruff: `E, F, I, UP, RUF, ANN401`, target `py313`, line length **88**, first-party group
  `pywire`.
- One class per file; relative imports inside `pywire`, absolute across packages.
- **Every commit must leave the repo green**: `uv run pytest`, `uv run ruff check .`,
  and `uv run pyright` all pass at every one of the six commits.
- **No version bump.** `pyproject.toml` stays at `0.4.0`; releasing is a separate,
  explicitly requested step.
- No new public names in `src/pywire/__init__.py`. `_Origin` is package-private.
- Tests never touch `container._registry` or any other private container state.

---

### Task 1: Extract `_build_from_class` (pure refactor, no behavior change)

`_create` currently does two things in one body: it manages the frame (stack push,
rollback mark, `ready` publication, pop) and it constructs the object. Task 2 adds a
second construction path, so the construction half must come out first. This repo's
rules require refactoring to be a separate commit from feature work, so this task
adds **no test and changes no behavior** — the existing suite is its test.

**Files:**
- Modify: `src/pywire/container.py:209-266` (`_create`)

**Interfaces:**
- Consumes: nothing.
- Produces: `Container._build_from_class(self, target_type: type, definition: BeanDefinition, requester: str | None, resolution: _Resolution) -> object` — allocates, publishes early, injects fields, runs `__init__`, returns the instance. Task 2 adds its sibling `_build_from_factory` and the branch that chooses between them.

- [ ] **Step 1: Record the green baseline**

Run: `uv run pytest`
Expected: PASS (98 tests). Write the number down; Task 1 must not change it.

- [ ] **Step 2: Replace the body of `_create`**

In `src/pywire/container.py`, replace the whole `_create` method with these two
methods, keeping every comment that travels with the code it explains:

```python
    def _create(
        self,
        target_type: type,
        definition: BeanDefinition,
        edge: _EdgeKind,
        requester: str | None,
        resolution: _Resolution,
    ) -> object:
        """Build, wire, and initialize a single bean."""
        created_mark = len(resolution.created)
        resolution.stack.append((target_type, edge))

        try:
            instance = self._build_from_class(
                target_type, definition, requester, resolution
            )

            # Last, and only on the success path: this is what the
            # unsynchronised fast path in resolve() reads.
            #
            # Guarded, because `instance` may no longer be the object this
            # frame built. clear_instances() takes the same reentrant lock this
            # frame already holds, so a component whose __init__ calls it wipes
            # the very definition under construction. Marking that emptied
            # definition ready would leave ready=True with instance=None -- the
            # one combination the fast path cannot detect -- and every later
            # resolve() would return None cast to T, silently and permanently.
            # The bean is left uncached instead; the caller still receives the
            # object this frame built.
            if definition.instance is instance:
                definition.ready = True

            return instance
        except BaseException:
            self._roll_back(resolution, created_mark)
            raise
        finally:
            resolution.stack.pop()

    def _build_from_class(
        self,
        target_type: type,
        definition: BeanDefinition,
        requester: str | None,
        resolution: _Resolution,
    ) -> object:
        """Allocate target_type, inject its fields, and run its __init__."""
        if definition.plan is None:
            # Planned before allocating: a class that cannot be planned is
            # never created and never published.
            definition.plan = self._plan(target_type, requester, resolution)

        # Cast because pyright resolves target_type.__new__ against the
        # metaclass, whose overloads describe creating a *class*, not
        # allocating an instance. The real signature is only known at
        # runtime anyway.
        allocate = cast(Callable[[type], object], target_type.__new__)
        instance = allocate(target_type)

        # Early publication: a dependency cycle closing through a field
        # finds this instance instead of recursing forever.
        definition.instance = instance
        resolution.created.append(definition)

        # Fields first, deliberately: by the time a component's __init__
        # body runs, its injected fields are set and readable. That is a
        # contract, and the only reason __new__ and __init__ are split.
        self._inject_fields(instance, definition.plan, target_type, resolution)
        kwargs = self._resolve_ctor_args(definition.plan, target_type, resolution)
        target_type.__init__(instance, **kwargs)

        return instance
```

- [ ] **Step 3: Verify nothing moved**

Run: `uv run pytest`
Expected: PASS, the same count as Step 1. If any test fails, the extraction changed
behavior — the most likely cause is the rollback mark or the stack pop drifting out
of `_create`; both must stay in the frame-management half.

- [ ] **Step 4: Lint and type-check**

Run: `uv run ruff check . && uv run pyright`
Expected: both clean.

- [ ] **Step 5: Commit**

```bash
git add src/pywire/container.py
git commit -m "♻️ Extract _build_from_class from _create"
```

---

### Task 2: The factory provider path

Adds the field that decides how a bean is obtained, the single registration
choke point, `register_factory`, both of its guards, and the invariant check on the
legal-cycle branch.

**Files:**
- Modify: `src/pywire/definitions.py` (add `_Origin`, two fields)
- Modify: `src/pywire/container.py` (imports, `_put`, `register`, `register_factory`, `_create` branch, `_build_from_factory`, `_resolve` guard)
- Create: `tests/test_registration_apis.py`
- Modify: `tests/test_container_semantics.py` (two new tests). Cycle *rejections*
  live here; `tests/test_circular_dependencies.py` covers only cycles that are legal.

**Interfaces:**
- Consumes: `Container._build_from_class` (Task 1).
- Produces:
  - `definitions._Origin` — `Enum` with `CLASS`, `INSTANCE`, `FACTORY`.
  - `BeanDefinition(cls, factory=None, origin=_Origin.CLASS, instance=None, ready=False, plan=None)`.
  - `Container._put(self, key: type, definition: BeanDefinition) -> None` — the one place the "already registered" rule lives. Tasks 3 and 4 call it.
  - `Container.register_factory[T](self, target_type: type[T], factory: Callable[[], T]) -> None`.
  - `Container._build_from_factory(self, target_type: type, factory: Callable[[], object], definition: BeanDefinition, requester: str | None, resolution: _Resolution) -> object`.

- [ ] **Step 1: Write the first failing tests**

Create `tests/test_registration_apis.py`:

```python
"""Behavior of register_factory and register_instance.

These tests describe the provider model from the outside: what a caller
observes, never how the registry stores it.
"""

import threading

import pytest

from pywire import Container, DependencyResolutionError, RegistrationError


class Engine:
    """Stand-in for a third-party object that is not zero-arg constructible."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn


def test_factory_is_not_called_until_something_resolves():
    container = Container()
    calls: list[int] = []

    def make_engine() -> Engine:
        calls.append(1)
        return Engine("postgres://")

    container.register_factory(Engine, make_engine)

    assert calls == []

    container.resolve(Engine)

    assert calls == [1]


def test_factory_is_called_once_and_its_result_is_the_singleton():
    container = Container()
    calls: list[int] = []

    def make_engine() -> Engine:
        calls.append(1)
        return Engine("postgres://")

    container.register_factory(Engine, make_engine)

    first = container.resolve(Engine)
    second = container.resolve(Engine)

    assert first is second
    assert calls == [1]


def test_factory_may_resolve_its_own_dependencies():
    container = Container()

    class Settings:
        dsn = "postgres://from-settings"

    container.register(Settings)
    container.register_factory(
        Engine, lambda: Engine(container.resolve(Settings).dsn)
    )

    assert container.resolve(Engine).dsn == "postgres://from-settings"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_registration_apis.py -v`
Expected: FAIL — `AttributeError: 'Container' object has no attribute 'register_factory'`.

- [ ] **Step 3: Add `_Origin` and the two fields to `BeanDefinition`**

In `src/pywire/definitions.py`, add the imports and the enum above the dataclass:

```python
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto


class _Origin(Enum):
    """How a bean's instance is obtained.

    Diagnostic only: no branch in the container reads this. It exists so a
    definition inspected in a debugger states what it is, and so the provider
    model is documented by the code and not only by prose.
    """

    CLASS = auto()
    INSTANCE = auto()
    FACTORY = auto()
```

Then extend the dataclass — `cls` keeps its position, the two new fields go before
the runtime state so the declaration reads "what this bean is" then "where it
currently stands":

```python
    cls: type
    factory: Callable[[], object] | None = None
    origin: _Origin = _Origin.CLASS
    instance: object | None = None
    ready: bool = False
    plan: InjectionPlan | None = None
```

Add to the class docstring's `Attributes:` block:

```
        factory: Callable that produces the instance, or None to construct `cls`
            the ordinary way. A pushed instance is a factory returning the object
            it was handed, which is what keeps clear_instances() and rollback
            free of special cases: every bean is rebuildable, and rebuilding a
            pushed one yields the same object.
        origin: How the instance is obtained. Read by humans, never by the
            container.
```

- [ ] **Step 4: Add `_put`, `register_factory`, and the construction branch**

In `src/pywire/container.py`, add `import inspect` to the imports, and
`from .definitions import BeanDefinition, _Origin`.

Replace `register`'s body with a call to the new choke point, and add the two new
methods next to it:

```python
    def register[T](self, cls: type[T]) -> type[T]:
        """Register a class as a component.

        The class is neither instantiated nor modified. Both happen lazily,
        inside resolve().
        """
        self._put(cls, BeanDefinition(cls=cls))

        return cls

    def register_factory[T](
        self, target_type: type[T], factory: Callable[[], T]
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

        Raises:
            RegistrationError: target_type is already registered, or factory is
                a coroutine function.
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

        self._put(
            target_type,
            BeanDefinition(
                cls=target_type, factory=factory, origin=_Origin.FACTORY
            ),
        )

    def _put(self, key: type, definition: BeanDefinition) -> None:
        """Store definition under key, refusing to displace an existing one.

        The single place the collision rule lives, for all three registration
        APIs. A registration can only ever *add* a key -- which is what makes it
        safe to register from inside a component's __init__ or a factory, where
        the reentrant lock lets the call through: no definition can be emptied
        under the frame that is building it.
        """
        with self._lock:
            if key in self._registry:
                name = getattr(key, "__name__", key)

                raise RegistrationError(
                    f"'{name}' is already registered in this container."
                )

            self._registry[key] = definition
```

Then branch in `_create` — replace the single `_build_from_class` call from Task 1
with:

```python
            factory = definition.factory

            if factory is None:
                instance = self._build_from_class(
                    target_type, definition, requester, resolution
                )
            else:
                instance = self._build_from_factory(
                    target_type, factory, definition, requester, resolution
                )
```

The factory is read into a local and passed down rather than re-read inside the
callee: that is what narrows `Callable[[], object] | None` to `Callable[[], object]`
for the type checker, without an assert.

Add the sibling method after `_build_from_class`:

```python
    def _build_from_factory(
        self,
        target_type: type,
        factory: Callable[[], object],
        definition: BeanDefinition,
        requester: str | None,
        resolution: _Resolution,
    ) -> object:
        """Call the registered factory and publish what it returns.

        Publication is necessarily *late* here: unlike the class path there is
        no allocated object to publish before user code runs. A cycle closing
        back on a factory bean therefore cannot find a partial instance -- which
        is exactly why every such cycle is rejected rather than tolerated, see
        _reject_constructor_cycle.
        """
        instance = factory()

        if instance is None:
            # The one value that would corrupt the ready/instance invariant
            # into ready=True with instance=None, which the lock-free fast path
            # cannot detect and would hand out forever.
            raise DependencyResolutionError(
                f"The factory registered for '{target_type.__name__}' "
                "returned None.",
                chain=resolution.chain(),
                requester=requester,
            )

        definition.instance = instance
        resolution.created.append(definition)

        return instance
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_registration_apis.py -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Write the failing guard tests**

Append to `tests/test_registration_apis.py`:

```python
def test_factory_returning_none_is_refused():
    container = Container()

    container.register_factory(Engine, lambda: None)  # type: ignore[arg-type,return-value]

    with pytest.raises(DependencyResolutionError, match="returned None"):
        container.resolve(Engine)


def test_async_factory_is_refused_at_registration():
    container = Container()

    async def make_engine() -> Engine:
        return Engine("postgres://")

    with pytest.raises(RegistrationError, match="coroutine function"):
        container.register_factory(Engine, make_engine)  # type: ignore[arg-type]


def test_registering_a_factory_for_a_taken_key_is_refused():
    container = Container()

    container.register_factory(Engine, lambda: Engine("a"))

    with pytest.raises(RegistrationError, match="is already registered"):
        container.register_factory(Engine, lambda: Engine("b"))


def test_factory_bean_is_rebuilt_after_clear_instances():
    container = Container()

    container.register_factory(Engine, lambda: Engine("postgres://"))

    first = container.resolve(Engine)
    container.clear_instances()
    second = container.resolve(Engine)

    assert first is not second


def test_a_failing_factory_leaves_nothing_cached_and_can_be_retried():
    container = Container()
    attempts: list[int] = []

    def flaky() -> Engine:
        attempts.append(1)

        if len(attempts) == 1:
            raise RuntimeError("boom")

        return Engine("postgres://")

    container.register_factory(Engine, flaky)

    with pytest.raises(RuntimeError, match="boom"):
        container.resolve(Engine)

    assert container.resolve(Engine).dsn == "postgres://"
    assert attempts == [1, 1]


def test_a_factory_bean_is_rolled_back_when_an_upstream_frame_fails():
    """Rollback is per-subtree, so a factory bean built inside a failing branch
    must not stay cached -- and must be rebuilt, not resurrected, afterwards."""
    container = Container()
    built: list[Engine] = []

    def make_engine() -> Engine:
        engine = Engine("postgres://")
        built.append(engine)
        return engine

    class Repo:
        engine: Autowired[Engine]

        def __init__(self) -> None:
            raise RuntimeError("upstream boom")

    container.register_factory(Engine, make_engine)
    container.register(Repo)

    with pytest.raises(RuntimeError, match="upstream boom"):
        container.resolve(Repo)

    second = container.resolve(Engine)

    assert len(built) == 2
    assert second is built[1]


def test_concurrent_resolution_calls_the_factory_once():
    """A widened window, not a race we hope to lose.

    The factory sleeps so every thread is inside resolve() while the first one
    is still building; a bare barrier reproduces a missing lock only about one
    run in six, which is too flaky to protect anything.
    """
    container = Container()
    calls: list[int] = []

    def slow_engine() -> Engine:
        calls.append(1)
        threading.Event().wait(0.05)
        return Engine("postgres://")

    container.register_factory(Engine, slow_engine)

    results: list[Engine] = []
    threads = [
        threading.Thread(target=lambda: results.append(container.resolve(Engine)))
        for _ in range(8)
    ]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join()

    assert calls == [1]
    assert len({id(result) for result in results}) == 1
```

- [ ] **Step 7: Run them to verify which fail**

Run: `uv run pytest tests/test_registration_apis.py -v`
Expected: the `clear_instances`, rollback and concurrency tests already PASS —
they are the point of the design, and they pass without a line written for them. The
`None` and `coroutine function` tests already pass too if Step 4 was applied in full;
if you implemented Step 4 without the two guards, they FAIL here. Either way, do not
move on until all ten pass.

- [ ] **Step 8: Write the failing cycle tests**

Append to `tests/test_container_semantics.py`. That file already imports
`pytest`, `Autowired`, `CircularDependencyError` and `Container`, so no import
changes are needed. (Do not put these in `tests/test_circular_dependencies.py`: that
file is about cycles the container *allows*.)

```python
def test_a_factory_resolving_its_own_bean_is_a_cycle():
    container = Container()

    class Client:
        pass

    container.register_factory(Client, lambda: container.resolve(Client))

    with pytest.raises(CircularDependencyError):
        container.resolve(Client)


def test_a_cycle_that_passes_through_a_factory_is_rejected():
    container = Container()

    class Client:
        pass

    class Service:
        client: Autowired[Client]

    container.register(Service)
    container.register_factory(Client, lambda: container.resolve(Service))

    with pytest.raises(CircularDependencyError):
        container.resolve(Client)
```

- [ ] **Step 9: Run them**

Run: `uv run pytest tests/test_container_semantics.py -v`
Expected: PASS. Both are rejected by the *existing* rule — a `resolve()` called from
inside a factory finds a non-empty stack and counts as a constructor edge — so no
container change is needed. If either one instead returns `None` or hangs, stop:
that means the edge kind assigned to a reentrant `resolve()` has drifted, and the
proof in the spec no longer holds.

- [ ] **Step 10: Add the invariant check on the legal-cycle branch**

In `_resolve`, the branch that returns a partially built partner currently reads
`return definition.instance`. Replace it with:

```python
        if position is not None:
            self._reject_constructor_cycle(
                target_type, position, edge, requester, resolution
            )

            if definition.instance is None:
                # Unreachable today: the only bean whose instance is still None
                # while its own frame is on the stack is one being built by a
                # factory, and every path back into a factory frame is a
                # constructor edge, rejected just above. Kept as an invariant
                # check rather than a proof in prose, because the proof rests on
                # "_EdgeKind has three values and only CTOR counts" -- whoever
                # adds a fourth will meet this line instead of silently
                # injecting None typed as T.
                cycle = resolution.chain_through(target_type, start=position)

                raise CircularDependencyError(
                    f"Circular dependency through the factory for "
                    f"'{target_type.__name__}': a factory cannot publish a "
                    "partial instance.",
                    chain=cycle,
                    requester=requester,
                )

            # A legal field cycle: the partner is still under construction, but
            # its identity is final, which is all a stored reference needs.
            return definition.instance
```

- [ ] **Step 11: Run the whole suite**

Run: `uv run pytest && uv run ruff check . && uv run pyright`
Expected: all green, 98 + 12 tests.

- [ ] **Step 12: Commit**

```bash
git add src/pywire/definitions.py src/pywire/container.py tests/test_registration_apis.py tests/test_container_semantics.py
git commit -m "✨ Add register_factory and the factory provider path"
```

---

### Task 3: `register_instance` as sugar over the factory path

**Files:**
- Modify: `src/pywire/container.py` (one method)
- Modify: `tests/test_registration_apis.py`

**Interfaces:**
- Consumes: `Container._put`, `BeanDefinition`, `_Origin` (Task 2).
- Produces: `Container.register_instance[T](self, instance: T) -> None`. Task 4 adds its `as_type` keyword.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_registration_apis.py`:

```python
class PostgresConfig:
    def __init__(self, host: str) -> None:
        self.host = host


class AppConfig:
    def __init__(self) -> None:
        self.postgres = PostgresConfig("db.internal")


def test_a_pushed_instance_is_returned_by_identity():
    container = Container()
    config = AppConfig()

    container.register_instance(config)

    assert container.resolve(AppConfig) is config


def test_the_default_key_is_the_runtime_type():
    container = Container()
    config = AppConfig()

    container.register_instance(config.postgres)

    assert container.resolve(PostgresConfig) is config.postgres


def test_pushing_none_is_refused():
    container = Container()

    with pytest.raises(RegistrationError, match="Cannot register None"):
        container.register_instance(None)


def test_a_pushed_instance_survives_clear_instances_by_identity():
    """The payoff of instance-as-factory: teardown stays uniform.

    A pushed object is not reconstructible by the container, so a design that
    dropped it on clear would leave a definition nobody can repopulate. The
    closure makes rebuilding it mean handing back the same object.
    """
    container = Container()
    config = AppConfig()

    container.register_instance(config)

    first = container.resolve(AppConfig)
    container.clear_instances()

    assert container.resolve(AppConfig) is first


def test_a_pushed_instance_is_not_wired():
    """The trap of this API, written down so it cannot surprise anyone.

    The container injects only into objects it constructs. This one arrived
    already built, so its Autowired field is simply absent.
    """
    container = Container()

    class Dependency:
        pass

    class Service:
        dep: Autowired[Dependency]

    container.register(Dependency)
    container.register_instance(Service())

    assert not hasattr(container.resolve(Service), "dep")


def test_pushing_over_a_taken_key_is_refused():
    container = Container()

    container.register_instance(AppConfig())

    with pytest.raises(RegistrationError, match="is already registered"):
        container.register_instance(AppConfig())
```

Add `Autowired` to this file's imports: `from pywire import Autowired, Container, ...`.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_registration_apis.py -v`
Expected: FAIL — `AttributeError: 'Container' object has no attribute 'register_instance'`.

- [ ] **Step 3: Implement `register_instance`**

Add to `src/pywire/container.py`, immediately after `register_factory`:

```python
    def register_instance[T](self, instance: T) -> None:
        """Register an already-built object as the singleton for its own type.

        For objects the container cannot build: a nested field of a loaded
        configuration, a client constructed at the entry point. The object is
        taken as it is -- **it is not wired**, because the container injects
        only into instances it constructs itself.

        Stored as a factory returning the object it was handed. That is what
        makes teardown uniform: clear_instances() drops the cached instance like
        any other, and rebuilding hands back the same object.

        Raises:
            RegistrationError: the key is already registered, or instance is
                None.
        """
        if instance is None:
            raise RegistrationError("Cannot register None as an instance.")

        target_type = type(instance)

        def pushed_instance() -> object:
            """Return the object handed to register_instance."""
            return instance

        self._put(
            target_type,
            BeanDefinition(
                cls=target_type,
                factory=pushed_instance,
                origin=_Origin.INSTANCE,
            ),
        )
```

The closure is **named**, not a lambda: a definition inspected in a debugger must
read `origin=INSTANCE, factory=<function pushed_instance ...>`, which is the whole
reason `origin` was kept.

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_registration_apis.py -v`
Expected: PASS (16 tests in this file).

- [ ] **Step 5: Full suite, lint, types**

Run: `uv run pytest && uv run ruff check . && uv run pyright`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add src/pywire/container.py tests/test_registration_apis.py
git commit -m "✨ Add register_instance for objects the container cannot build"
```

---

### Task 4: `as_type=` supertype binding

**Files:**
- Modify: `src/pywire/container.py` (`register`, `register_instance` signatures)
- Modify: `tests/test_registration_apis.py`

**Interfaces:**
- Consumes: `Container._put`, `register`, `register_instance` (Tasks 2-3).
- Produces:
  - `Container.register[T](self, cls: type[T], *, as_type: type | None = None) -> type[T]`
  - `Container.register_instance(self, instance: object, *, as_type: type | None = None) -> None`

  `register_instance` carries **no** type parameter. It had one while `as_type` was
  annotated `type[T]`; once that was withdrawn, `T` had a single use and pyright
  reports `reportInvalidTypeVarUse` ("appears only once ... use object instead").
  `register[T]` and `register_factory[T]` keep theirs — there `T` appears twice.

  `as_type` is a bare `type`, not `type[T]`: `type[T]` reads as a constraint but is
  not one - a type checker solves `T` to the join of the two arguments and accepts an
  unrelated class (measured on this project's pyright). The bare annotation says what
  is true.

  Both **rebind**: the definition is stored under `as_type` and under nothing else.
  Task 5 calls `register(cls, as_type=...)` from the decorators.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_registration_apis.py`:

```python
class UserRepository(Protocol):
    def name(self) -> str: ...


class PostgresUserRepo:
    def name(self) -> str:
        return "postgres"


def test_a_class_bound_to_a_supertype_resolves_under_that_supertype():
    container = Container()

    container.register(PostgresUserRepo, as_type=UserRepository)

    assert isinstance(container.resolve(UserRepository), PostgresUserRepo)


def test_as_type_rebinds_and_does_not_add():
    """The concrete type is no longer a key: consumers must use the abstraction."""
    container = Container()

    container.register(PostgresUserRepo, as_type=UserRepository)

    with pytest.raises(DependencyResolutionError, match="not registered"):
        container.resolve(PostgresUserRepo)


def test_a_rebound_class_is_still_built_and_wired_by_the_container():
    container = Container()

    class Dependency:
        pass

    class Repo:
        dep: Autowired[Dependency]

        def name(self) -> str:
            return "repo"

    container.register(Dependency)
    container.register(Repo, as_type=UserRepository)

    resolved = container.resolve(UserRepository)

    assert isinstance(resolved, Repo)
    assert resolved.dep is container.resolve(Dependency)


def test_an_autowired_field_resolves_through_the_supertype():
    container = Container()

    class Service:
        repo: Autowired[UserRepository]

    container.register(PostgresUserRepo, as_type=UserRepository)
    container.register(Service)

    assert container.resolve(Service).repo.name() == "postgres"


def test_a_pushed_instance_can_be_bound_to_a_supertype():
    container = Container()
    repo = PostgresUserRepo()

    container.register_instance(repo, as_type=UserRepository)

    assert container.resolve(UserRepository) is repo


def test_two_implementations_cannot_claim_the_same_supertype():
    container = Container()

    class OtherRepo:
        def name(self) -> str:
            return "other"

    container.register(PostgresUserRepo, as_type=UserRepository)

    with pytest.raises(RegistrationError, match="is already registered"):
        container.register(OtherRepo, as_type=UserRepository)
```

Add `from typing import Protocol` to this file's imports.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_registration_apis.py -v`
Expected: FAIL — `TypeError: register() got an unexpected keyword argument 'as_type'`.

- [ ] **Step 3: Add the keyword to both methods**

In `src/pywire/container.py`, change `register`:

```python
    def register[T](self, cls: type[T], *, as_type: type | None = None) -> type[T]:
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
        """
        self._put(as_type if as_type is not None else cls, BeanDefinition(cls=cls))

        return cls
```

and `register_instance` — replace its signature and its `_put` call, leaving the
`None` guard and the named closure exactly as they are:

```python
    def register_instance(
        self, instance: object, *, as_type: type | None = None
    ) -> None:
```

```python
        self._put(
            as_type if as_type is not None else target_type,
            BeanDefinition(
                cls=target_type,
                factory=pushed_instance,
                origin=_Origin.INSTANCE,
            ),
        )
```

Add to `register_instance`'s docstring, under `Args:`:

```
            as_type: Key to register the object under, instead of its runtime
                type. Needed when that type is a generated subclass -- a mock, a
                proxy -- or when consumers should depend on an abstraction. Not
                checked against the object's own type; see register().
```

Note `cls=target_type` stays the **concrete** type in both: the key lives in the
registry dict, the definition keeps saying what the object actually is.

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_registration_apis.py -v`
Expected: PASS (22 tests in this file).

- [ ] **Step 5: Add the FastAPI regression test**

In `tests/test_fastapi_integration.py`, add `from typing import Protocol` to the
imports (the file already imports `pytest`, `FastAPI`, `TestClient`, `Autowired`,
`Container` and `wire`), then add:

```python
def test_a_protocol_bound_dependency_resolves_in_a_route():
    """as_type makes a route parameter's annotation a Protocol.

    _wire_endpoint rewrites it to `annotation=target` plus Depends(...), and
    FastAPI never validates the annotation of a Depends-defaulted parameter --
    verified against this project's FastAPI before the design was written.
    """
    container = Container()

    class Greeter(Protocol):
        def greet(self) -> str: ...

    class ItalianGreeter:
        def greet(self) -> str:
            return "ciao"

    container.register(ItalianGreeter, as_type=Greeter)

    app = FastAPI()
    wire(app, container=container)

    @app.get("/greet")
    def greet_route(greeter: Autowired[Greeter]) -> dict[str, str]:
        return {"greeting": greeter.greet()}

    response = TestClient(app).get("/greet")

    assert response.status_code == 200
    assert response.json() == {"greeting": "ciao"}
```

- [ ] **Step 6: Run it**

Run: `uv run pytest tests/test_fastapi_integration.py -v`
Expected: PASS. If it fails with a pydantic schema error, stop and report — the
spec's DIP story depends on this and the probe said it holds.

- [ ] **Step 7: Full suite, lint, types**

Run: `uv run pytest && uv run ruff check . && uv run pyright`
Expected: all green.

- [ ] **Step 8: Commit**

```bash
git add src/pywire/container.py tests/test_registration_apis.py tests/test_fastapi_integration.py
git commit -m "✨ Add as_type supertype binding to register and register_instance"
```

---

### Task 5: Dual-form component decorators

**Files:**
- Modify: `src/pywire/decorators.py`
- Modify: `tests/test_components.py`

**Interfaces:**
- Consumes: `Container.register(cls, as_type=...)` (Task 4).
- Produces: `component` usable bare (`@component`) or called with a required keyword (`@component(as_type=X)`); `service`, `repository`, `agent`, `client` inherit both forms because they are the same function object.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_components.py`:

```python
def test_the_parameterized_decorator_binds_to_a_supertype():
    from typing import Protocol

    class Greeter(Protocol):
        def greet(self) -> str: ...

    @repository(as_type=Greeter)
    class ItalianGreeter:
        def greet(self) -> str:
            return "ciao"

    resolved = get_default_container().resolve(Greeter)

    assert isinstance(resolved, ItalianGreeter)


def test_the_parameterized_decorator_returns_the_decorated_class():
    class Marker:
        pass

    @component(as_type=Marker)
    class Concrete(Marker):
        pass

    assert Concrete.__name__ == "Concrete"
    assert issubclass(Concrete, Marker)


def test_calling_the_decorator_with_no_arguments_is_an_error():
    with pytest.raises(TypeError, match="as_type"):

        @component()
        class Service:
            pass
```

Ensure `repository` and `component` are imported in this file (`component` already
is; add `repository` to the import list if missing).

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_components.py -v`
Expected: FAIL — `TypeError: component() takes 1 positional argument but 0 were given`
on the first two, and the third fails for the wrong reason (wrong message).

- [ ] **Step 3: Implement the dual form**

Replace `component` in `src/pywire/decorators.py`:

```python
from collections.abc import Callable
from typing import overload


@overload
def component[T](cls: type[T]) -> type[T]: ...


@overload
def component[T](*, as_type: type) -> Callable[[type[T]], type[T]]: ...


def component[T](
    cls: type[T] | None = None, *, as_type: type | None = None
) -> type[T] | Callable[[type[T]], type[T]]:
    """Decorator to register a class on the default container.

    Usable bare or called with a binding:

        @service
        class UserService: ...

        @repository(as_type=UserRepository)
        class PostgresUserRepo: ...

    The called form **rebinds** the registration key, exactly as
    Container.register(cls, as_type=...) does: the decorated class is no longer
    a key of its own.

    Unlike Container.register, this path does not check the subtype relation
    statically. Python cannot express "a TypeVar bounded by another TypeVar", so
    the choice is between checking the relation and preserving the decorated
    class's own type for callers -- and the latter is worth more.

    Raises:
        TypeError: called with parentheses but without as_type.
    """
    if cls is not None:
        get_default_container().register(cls)

        return cls

    if as_type is None:
        # A single def cannot make a keyword required only in the called form,
        # so the overloads make this a static error and this makes it a runtime
        # one. Nothing is gained by treating @component() as @component: it is
        # a typo, not a shorthand.
        raise TypeError(
            "component() requires 'as_type' when called with parentheses. "
            "Use @component without parentheses to register under the class "
            "itself."
        )

    binding = as_type

    def decorate(target: type[T]) -> type[T]:
        get_default_container().register(target, as_type=binding)

        return target

    return decorate
```

The alias assignments at the bottom of the file (`service = component`, and the
rest) are unchanged and pick up both forms for free — which is exactly why the
spec spells it out: it stops being free the day someone gives an alias its own
behavior.

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest tests/test_components.py -v`
Expected: PASS.

- [ ] **Step 5: Full suite, lint, types**

Run: `uv run pytest && uv run ruff check . && uv run pyright`
Expected: all green. Pyright must accept `@repository(as_type=Greeter)` above a
class that structurally satisfies `Greeter`, and must still report the decorated
name as its own class — if it reports `type[Greeter]`, the overloads are wrong.

- [ ] **Step 6: Commit**

```bash
git add src/pywire/decorators.py tests/test_components.py
git commit -m "✨ Let component decorators bind to a supertype"
```

---

### Task 6: Documentation

**Files:**
- Modify: `README.md` (new section before `## FastAPI Integration`)
- Modify: `CLAUDE.md` (registration/resolution flow, module table, decorators bullet)

**Interfaces:**
- Consumes: everything above. Produces no code.

- [ ] **Step 1: Add the composition-root section to `README.md`**

Insert immediately before `## FastAPI Integration`:

````markdown
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
````

- [ ] **Step 2: Update `CLAUDE.md`**

Under **Registration & resolution flow**, after the `container.register(cls)`
bullet, add:

```markdown
- Three registration APIs share one choke point, `Container._put`, which owns the
  "already registered" rule: `register(cls, as_type=None)`,
  `register_instance(obj, as_type=None)`, `register_factory(target_type, factory)`.
  `as_type` **rebinds** the key rather than adding one — one registration, one key
  — and the subtype relation is checked statically by the generic signature, never
  by `issubclass`, which cannot check a structural `Protocol`.
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
  `register_factory`. Everything else about a binding is the type checker's job.
```

In the **module layout** table, replace the `container.py` and `definitions.py`
rows:

```markdown
| `container.py` | `Container`: registry, register/register_instance/register_factory/resolve/get/clear_instances, `_put`, `_Resolution`, `_build_from_class` and `_build_from_factory`, per-subtree rollback, lock |
| `definitions.py` | `BeanDefinition`: registration metadata, `factory`, `origin`, singleton slot, `ready` flag, cached `InjectionPlan`; `_Origin` |
```

In the **Component decorators** section, replace the first bullet's last sentence
("It takes no container argument by design — use `container.register(cls)` directly
when an explicit container is needed.") with:

```markdown
  It takes no container argument by design — use `container.register(cls)` directly
  when an explicit container is needed. It is dual-form: bare (`@service`) or called
  with a required `as_type` keyword (`@repository(as_type=UserRepository)`), which
  rebinds the key exactly as `Container.register` does. `@component()` with empty
  parentheses is a `TypeError`. On this path the subtype relation is *not* checked
  statically: preserving the decorated class's own type for callers is worth more,
  and Python cannot express both.
```

- [ ] **Step 3: Verify the docs describe the code that exists**

Run: `uv run pytest && uv run ruff check . && uv run pyright`
Expected: green. Then re-read the README block you inserted against
`src/pywire/container.py` — every method name, keyword and error in it must exist.

- [ ] **Step 4: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "📝 Document the push primitive and supertype binding"
```

---

## Done criteria

- `uv run pytest`, `uv run ruff check .`, `uv run pyright` all pass.
- Each of the six commits, checked out on its own, has a green `uv run pytest`.
- `grep -rn "_registry" tests/` returns nothing — no test reaches into container
  internals.
- `grep -rn "lambda: instance\|lambda: obj" src/` returns nothing — the pushed
  closure is named.
- `from pywire import _Origin` raises `ImportError`; `from pywire.definitions import
  _Origin` works.
- `pyproject.toml` still says `0.4.0`.

## Follow-up (not this plan)

- **`configuration` as a decorator alias.** One line in `decorators.py`
  (`configuration = component`), its own commit, decided separately. It shares
  nothing with this plan.
- **Qualifiers.** Ruled out here. Revisit only with a case that distinct types
  cannot express.
- **`register_instance` of an object whose class declares `Autowired` fields.**
  Accepted as documented-and-tested behavior on the assumption that only
  configuration and third-party objects are pushed. If that assumption breaks,
  the fix is a registration-time check that those fields are *present* on the
  object (`hasattr`), which permits a pre-wired object while catching the mistake —
  and which needs a fields-only inspection exposed from `plans.py`, because
  `InjectionPlan.for_class` would reject an `Engine` outright for an `__init__` the
  container never calls.
