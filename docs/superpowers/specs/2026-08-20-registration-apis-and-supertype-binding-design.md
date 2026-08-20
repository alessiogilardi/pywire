# Registration APIs: the push primitive and supertype binding

Date: 2026-08-20 (revised the same day after a design grilling)
Status: Approved for implementation

This is **spec 2 of 2**. Spec 1
(`2026-08-18-container-resolve-time-wiring-design.md`, released as `v0.4.0`) made
`resolve()` the single construction site. This spec adds the two things that site
was left unable to express: obtaining a bean the container did **not** construct
(`register_instance`, `register_factory`), and binding a bean to a key other than
its own class (`as_type=`).

## Motivation

Two capabilities are missing from `v0.4.0`, both hit by real code.

**1. Objects the container cannot construct.** `resolve()` builds a bean by calling
`cls()` with no arguments. That covers a component the user wrote; it does not cover:

- A nested field of an already-loaded configuration. `AppConfig.postgres` is a
  `PostgresConfig` that exists only as an attribute of its root — `PostgresConfig()`
  in isolation loads nothing. Publishing it as a bean of its own type is what lets a
  repository declare `Autowired[PostgresConfig]` instead of `Autowired[AppConfig]`,
  which is interface segregation applied to configuration.
- Third-party objects that are not zero-argument constructible, or whose
  construction depends on a value that only exists at the entry point:
  `create_engine(dsn)`, `httpx.AsyncClient(base_url=...)`.

The library already admits the gap: two error messages written in spec 1
(`plans.py`) tell the user to "register a pre-built instance instead" — an
instruction that currently has no API behind it.

**2. Dependency Inversion is inexpressible.** The registry is keyed by exact type
with no MRO walk, so `Autowired[UserRepository]` where `UserRepository` is a
`Protocol` or an ABC cannot resolve: only the concrete `PostgresUserRepo` is a key.
Every consumer must therefore name the implementation it depends on.

**Goal:** let the composition root push objects into the container, and let a
registration choose its own key — without adding a second injection engine and
without weakening any invariant spec 1 established.

## Non-goals

- **Qualifiers / named beans.** The key stays the type. Two sub-configurations of
  the same type (`config.primary_db` and `config.replica_db`, both
  `PostgresConfig`) cannot both be registered: the second raises
  `RegistrationError`. The workaround is two distinct types, or two distinct
  `as_type=` bindings. This is a real limitation of the design, accepted
  deliberately — see "Alternatives considered".
- **Implicit subtype scanning.** `as_type=` is always written by hand. Registering
  `PostgresUserRepo` never makes it resolvable as `UserRepository` by inference.
- **`@provides` / `@configuration` with `@Bean`-style methods.** A factory whose
  parameters are injected is a second injection engine alongside `plans.py`.
  Out of scope, as in spec 1.
- **`PROTOTYPE` scope.** Every bean remains a singleton per container.
- **Overriding an existing registration.** No `replace=` flag. A different object
  graph is a different `Container()`.
- **Wiring pushed objects.** The container injects only into objects it constructs.
- **`configuration` as a decorator alias.** A one-line change to `decorators.py`
  that shares nothing with this spec; if wanted, it gets its own commit.
- **`unregister(key)`.** No use case, and it would reopen the question of what
  happens to instances already injected elsewhere.
- **`is_registered(key)` / `__contains__`.** It invites "ask, then resolve", which
  is not even atomic under the lock.
- **Chaining.** `register_instance` and `register_factory` return `None`. A caller
  who needs the object in hand assigns it on its own line first. The resulting
  asymmetry — `register` returns `cls`, the other two return `None` — is deliberate:
  `register` must work as a decorator, and nobody writes
  `@container.register_instance` above a class.

## Chosen approach

One new field decides how a bean is obtained; `register_instance` is sugar over the
factory path.

```python
class _Origin(Enum):        # private, like _EdgeKind
    CLASS = auto()
    INSTANCE = auto()
    FACTORY = auto()

@dataclass(slots=True)
class BeanDefinition:
    cls: type                                    # concrete class: construction + diagnostics
    factory: Callable[[], object] | None = None  # None -> construct cls
    origin: _Origin = _Origin.CLASS              # messages and repr only; no behavior
    instance: object | None = None
    ready: bool = False
    plan: InjectionPlan | None = None
```

`factory is None` selects today's path (plan → `__new__` → early publication →
field injection → `__init__`). `factory is not None` selects a new, much shorter
one: call it, reject `None`, publish, mark ready.

`register_instance(obj)` is `register_factory(type(obj), lambda: obj)` in everything
that affects behavior; the two differ only in the `origin` they record. That is the
whole trick, and it is what keeps the rest of the container unchanged — see "Why
instance-as-factory" below.

## Public surface

```python
class Container:
    def register[T](self, cls: type[T], *, as_type: type | None = None) -> type[T]: ...
    def register_instance(self, instance: object, *, as_type: type | None = None) -> None: ...
    def register_factory[T](self, target_type: type[T], factory: Callable[[], T]) -> None: ...
```

```python
# main.py -- the composition root, the only place the container is named
config = AppConfig(_yaml_file=args.config)     # fails fast, here, before the port opens
container = get_default_container()

container.register_instance(config)            # key: AppConfig (only if anyone autowires it)
container.register_instance(config.postgres)   # key: PostgresConfig
container.register_factory(Engine, lambda: create_engine(config.postgres.dsn))
container.register_factory(httpx.AsyncClient, httpx.AsyncClient)

# everywhere else: no mention of the container
@repository(as_type=UserRepository)
class PostgresUserRepo:
    db: Autowired[PostgresConfig]
    engine: Autowired[Engine]

@service
class UserService:
    repo: Autowired[UserRepository]            # does not know PostgresUserRepo exists
```

### Semantics

1. **`register_factory` has no `as_type`.** Its first parameter *is* the key, so the
   binding is already explicit. `as_type` exists only where the key would otherwise
   be implicit: the class in `register`, `type(instance)` in `register_instance`.

2. **`as_type` rebinds; it does not add.** After
   `register(PostgresUserRepo, as_type=UserRepository)` there is **one** definition
   under **one** key, and `resolve(PostgresUserRepo)` raises
   `DependencyResolutionError` like any unregistered type. A caller who wants both
   keys registers twice, explicitly, and knows they are creating two beans.

3. **Default key of `register_instance` is `type(instance)`.** Correct for
   `config.postgres` (`PostgresConfig`). For an object whose runtime type is a
   generated subclass — a mock, a proxy — the default key would be surprising;
   that is exactly when `as_type` is passed.

4. **The binding is not checked — not statically, not at runtime.** Writing
   `as_type: type[T]` beside `cls: type[T]` looks like it constrains the two. It
   does not: a type checker solves `T` to the *join* of the two arguments — `object`
   in the worst case — and accepts an unrelated class. Measured against this
   project's pyright before implementation, together with the same question for
   `register_factory`: `register(Unrelated, as_type=Greeter)`,
   `register_factory(Engine, an_async_def)`, `register_factory(Engine, lambda: Other())`
   and `register_factory(Engine, lambda: None)` all type-check clean.
   `typing.NoInfer`, which would pin `T` to `as_type` and make the relation
   checkable, is absent from this project's Python (3.13.7) and reachable only
   through `typing_extensions` — a runtime dependency the library refuses. The
   signature therefore stops pretending: `as_type: type | None`, the same bare
   `type` the decorators use.

   Nor is the binding checked at runtime, and that is a separate decision with its
   own reason. `issubclass` cannot check a structural `Protocol` at all, and a
   `Protocol` is the main reason `as_type` exists — so the check would be silent
   exactly where mis-binding is most likely, while lending confidence in the
   nominal case. Its failure mode is also ordinary: a wrong binding yields a wrong
   *object*, which raises `AttributeError` at the first call on it. That is a normal
   Python programming error, not the silent `None`-typed-as-`T` class this library
   polices — which is precisely why the two rejections in point 5 are treated
   differently.

5. **Two runtime rejections: `None`, and async factories.** `register_instance(None)`
   raises `RegistrationError`; a factory returning `None` raises
   `DependencyResolutionError` at resolve time. `None` is singled out because it is
   the one value that would corrupt the `ready`/`instance` invariant into
   `ready=True, instance=None` — the "silent `None` typed as `T`" failure class this
   redesign exists to eliminate.

   `register_factory` additionally rejects a coroutine function
   (`inspect.iscoroutinefunction(factory)`) with `RegistrationError`. Without it,
   `async def make_client()` passes every check: calling it returns a coroutine
   object, which is not `None`, so a *coroutine* is published as the bean and
   injected everywhere, surfacing as an `AttributeError` on a coroutine plus a
   `RuntimeWarning: coroutine was never awaited` emitted at an unrelated moment.
   This check is redundant with nothing: `register_factory(Engine, make_engine)`
   where `make_engine` is an `async def` type-checks clean, for the same join reason
   as point 4. It is the only thing standing between an `async def` and a coroutine
   published as a bean. It earns its place where an `isinstance` check does not, on
   two grounds: `iscoroutinefunction` is *reliable* and uniform, whereas `isinstance`
   is structurally unable to check `Protocol`s; and the failure it prevents does not
   produce a wrong object, it produces something that is not an object at all. The
   check is knowingly **partial**: a callable object whose `__call__` is async is
   not detected.

6. **Key collision is always `RegistrationError`**, for all three APIs, implemented
   once in a private `_put(key, definition)`.

7. **A pushed object is never wired.** The container injects only into objects it
   constructs. `register_instance(MyService())` where `MyService` declares
   `Autowired` fields yields a permanently unwired object. This follows from spec
   1's rule that a hand-written `Cls()` is plain Python, but it is the trap of this
   API and is documented and tested as such. Irrelevant for configuration objects
   and third-party clients, which declare no `Autowired` fields.

8. **Decorators are dual-form.** `@repository` and
   `@repository(as_type=UserRepository)` are both valid, via `overload`. On the
   decorator path the decorated class keeps its own identity for the type checker
   and the static subtype check is given up — Python cannot express "a TypeVar
   bounded by another TypeVar", so the two are mutually exclusive, and identity is
   worth more — and since the measurement in point 4 shows `container.register` has
   no static check either, this path gives up nothing the other one had. Three
   details,
   fixed here so the implementation does not invent them: `as_type` is a **required**
   keyword in the called form, so `@repository()` raises `TypeError`. The `overload`
   pair makes that a static error for free; at runtime it costs one explicit check,
   because a single `def` cannot express "required only when called with
   parentheses". `as_type` is the **only** parameter, and
   the docstring promises no extensibility (`name=` would be the qualifier this spec
   rules out, and `lazy=` is meaningless in a container where everything is already
   lazy); **all five aliases** get the form, free of charge, since they are the same
   function object — which is exactly why the spec states it, as "free" turns into a
   bug the day someone differentiates an alias.

## Internal mechanics

### Registration

```python
def _put(self, key: type, definition: BeanDefinition) -> None:
    with self._lock:
        if key in self._registry:
            name = getattr(key, "__name__", key)
            raise RegistrationError(f"'{name}' is already registered in this container.")
        self._registry[key] = definition
```

The message loses today's `Component ` prefix: with `as_type` the key can be a
`Protocol`, which is not a component. The existing test matches
`"is already registered"` and is unaffected.

`cls` stays the **concrete** class even when the key is a supertype; the key lives
in the dict, not in the definition. For a factory bean `cls = target_type` and
serves diagnostics only, and `plan` stays `None` forever — a factory bean is never
planned.

### Construction

```python
try:
    if definition.factory is None:
        instance = self._build_from_class(target_type, definition, requester, resolution)
    else:
        instance = self._build_from_factory(target_type, definition, requester, resolution)

    if definition.instance is instance:      # today's reentrancy guard, unchanged
        definition.ready = True

    return instance
except BaseException:
    self._roll_back(resolution, created_mark)
    raise
finally:
    resolution.stack.pop()
```

`_build_from_class` is today's body, moved wholesale. The new path:

```python
def _build_from_factory(self, target_type, definition, requester, resolution) -> object:
    instance = definition.factory()

    if instance is None:
        raise DependencyResolutionError(
            f"The factory registered for '{target_type.__name__}' returned None.",
            chain=resolution.chain(), requester=requester,
        )

    definition.instance = instance           # late publication: it did not exist before
    resolution.created.append(definition)
    return instance
```

The structural difference to keep in mind: on the class path publication is
**early**, which is what lets a field cycle close; on the factory path it is
necessarily **late**, because the object does not exist until the factory returns.

### What does not change, and why that is the point

- **`_roll_back`**: zero lines. A factory definition is appended to
  `resolution.created` like any other, so a failing subtree clears it identically.
  Consequence to document: after an upstream failure the factory **runs again** on
  the next resolve, so a factory that builds a resource builds it twice — the same
  caveat already documented for `__init__` side effects.
- **`clear_instances()`**: zero lines, and this is the payoff of instance-as-factory
  (below).
- **The `ready`-gated fast path**: zero lines.
- **`InjectionPlan` / `plans.py` / `markers.py`**: untouched.

### Why instance-as-factory

A pushed instance is not reconstructible by the container: there is no `cls()` to
call. Under a design with three distinct provenances, `clear_instances()` empties a
definition nobody can repopulate, and the next `resolve()` either fails or — worse —
falls back to the class path and silently builds a **second, different**
`AppConfig`. Avoiding that requires an asymmetry ("clear skips INSTANCE beans") that
has to be documented, tested, and remembered.

Capturing the object in a closure removes the problem instead of handling it:

```python
container.register_instance(config)
a = container.resolve(AppConfig)
container.clear_instances()
assert container.resolve(AppConfig) is a        # the closure returns the same object

container.register_factory(Engine, make_engine)
e1 = container.resolve(Engine)
container.clear_instances()
assert container.resolve(Engine) is not e1      # the factory runs again -- as intended
```

Every bean is rebuildable, so teardown stays uniform and no branch is added to
`_roll_back` or `clear_instances`.

### Cycles through a factory: no new mechanism

A factory bean has no dependencies the container resolves on its behalf, so the only
way to re-enter its frame is a `resolve()` called **inside the factory body**. That
re-entrant public `resolve()` finds a non-empty stack and is therefore
`_EdgeKind.CTOR`. Two cases:

- the factory resolves itself: `stack[position + 1:]` is empty and the incoming edge
  is `CTOR` → rejected;
- the factory resolves `Other`, which comes back to the bean through a field:
  `stack[position + 1:]` holds `Other`'s frame, entered with `CTOR` → rejected.

Both raise `CircularDependencyError` instead of injecting the `None` that
`definition.instance` would hold. The existing rule — a cycle is legal only if every
edge is a field edge — covers the factory path unchanged, because a factory bean can
never be published early.

**Defensive guard (decided: include).** `_resolve`'s legal-cycle branch returns
`definition.instance`, which for a factory bean would be `None`. The proof above says
that branch is unreachable for factory beans, so the three lines
`if definition.instance is None: raise CircularDependencyError(...)` are dead code by
construction — which sits awkwardly next to this repo's rule that dead code gets
removed. They are included anyway, on the argument that "dead code" is the wrong
category: this is an **invariant check**, placed exactly where the invariant could
break silently, and its value is in the future rather than today. The proof depends
on "`_EdgeKind` has three values and only `CTOR` counts". Whoever adds a fourth edge
kind two years from now will not re-read the proof in this document; they will find
three lines telling them what they just broke. Same purchase as the `ready` guard in
`_create`, which likewise protects against a caller nobody sane writes.

### Thread safety

The three `register_*` take the lock, like `register`. **A factory runs under the
lock**, exactly as user `__init__` bodies do: same guarantee (one singleton per key)
and same cost — but the cost deserves louder documentation here, because factories
commonly do I/O. Opening a connection inside a factory serializes every resolution
in that container for its duration, and a factory that waits on another thread's
`resolve()` deadlocks.

Registering during an in-flight resolution (a factory calling `register_instance`)
stays legal and safe: the lock is reentrant, and with "collision is an error" a
registration can only **add** keys — it can never empty a definition under the frame
building it.

## Errors

No new exception classes; `__init__.py` is unchanged (`_Origin` is private).

| Situation | Exception | Message |
|---|---|---|
| key already registered (all three APIs) | `RegistrationError` | `'UserRepository' is already registered in this container.` |
| `register_instance(None)` | `RegistrationError` | `Cannot register None as an instance.` |
| `register_factory` with an `async def` | `RegistrationError` | `The factory for 'AsyncClient' is a coroutine function: it would publish a coroutine as the bean.` |
| factory returns `None` | `DependencyResolutionError` | `The factory registered for 'Engine' returned None.` |
| `resolve(concrete)` after rebinding | `DependencyResolutionError` | existing message, unchanged |
| cycle through a factory | `CircularDependencyError` | existing message, unchanged |
| factory raises | *propagates as-is* | rollback applied, no wrapping |

The last row is a consistency choice: spec 1 does not wrap exceptions raised by user
`__init__` bodies, and a factory is user code on the same footing.

## Testing

New file `tests/test_registration_apis.py`, plus additions to
`tests/test_container_semantics.py` (cycles — that file is where every
`CircularDependencyError` rejection lives; `tests/test_circular_dependencies.py`
covers only cycles the container allows) and `tests/test_components.py`
(collisions, decorators).

**Identity and keys** — the factory is not called until something resolves; it is
called exactly once; `resolve` returns the pushed object by identity (`is`, not
`==`); the default key is `type(instance)`; after `as_type` the concrete type no
longer resolves.

**The provider model, executable** — after `clear_instances()`: a pushed bean gives
`a is b`; a factory bean gives `e1 is not e2`. These two tests are the specification
of the design.

**The trap, written as a test** — `register_instance` of an object with `Autowired`
fields returns an object that is **not** wired.

**Failures** — factory returns `None`; factory raises (exception propagates, the
definition is left empty, the next resolve retries); a factory bean inside a subtree
that fails upstream (rolled back, then rebuilt).

**Cycles** — factory resolving itself; factory → `Other` → field back. Both
`CircularDependencyError`, never an injected `None`.

**Concurrency** — N threads resolving one factory bean call the factory once. Reuse
the technique already adopted for the default container: widen the window
deterministically (a sleeping factory) rather than racing for it.

**Decorators** — bare `@repository` and `@repository(as_type=UserRepository)` both
register; the bare form's behavior is byte-for-byte today's.

**Async factory** — `register_factory` with an `async def` raises at registration;
the coroutine never reaches the registry.

**FastAPI + `Protocol`: verified, not assumed.** `fastapi.py` rewrites a parameter to
`annotation=target` plus `Depends(...)`, so `as_type=SomeProtocol` makes `target` a
`Protocol`. Probed against this project's environment before writing this revision: a
bare `Protocol` annotation with a `Depends` default returns 200, a `@runtime_checkable`
one returns 200, and `/openapi.json` builds successfully — FastAPI does not treat a
`Depends`-defaulted parameter as a request field, so it never validates the
annotation. This is therefore an ordinary regression test in
`tests/test_fastapi_integration.py`, not a risk the plan must retire early.

## Documentation

- **README**: a composition-root section — the two real drivers, the "who
  constructs the object" rule, and a **"when you do not need push"** subsection: a
  `BaseSettings` with its own `env_prefix` is zero-argument constructible and can
  simply be registered, which already works today. Push is required when
  configuration depends on entry-point input (CLI arguments, a file chosen at
  runtime, values already in memory). It must also spell out the **two-beans-of-one-type
  workaround** rather than leaving it implicit in a non-goal: `primary_db` and
  `replica_db`, both `PostgresConfig`, cannot both be registered, and the way out is a
  distinct type in the configuration model (`class ReplicaDbConfig(PostgresConfig)`).
  The failure is loud and immediate — `RegistrationError` at startup, never a wrong
  bean injected quietly — which is what makes the limitation affordable.
- **CLAUDE.md**: update the `container.py` and `definitions.py` rows of the module
  table; add the provider model, the `as_type` policy (rebinding, never scanning),
  and the factory-cycle proof under "Registration & resolution flow"; reword the
  decorators' "It takes no container argument by design", which the parameterized
  form makes stale.
- **Version**: no bump. Only on explicit request; when it happens it is a `minor`
  (purely additive API).

## Alternatives considered

**Registering under both keys** (concrete *and* supertype, two dict entries sharing
one `BeanDefinition`). Rejected: `Autowired[Concrete]` would keep working, so nothing
would push consumers toward the abstraction, and swapping the implementation would
break every consumer that autowired the concrete type. Rebinding also composes — a
caller who genuinely wants both registers twice — whereas dual-keying cannot be
opted out of.

**An explicit provenance enum driving behavior** (`CLASS | FACTORY | INSTANCE`, with
`clear_instances()` skipping `INSTANCE`). Rejected in favor of instance-as-factory:
same observable behavior, one branch instead of three, and no asymmetry to remember.
`origin` survives from this alternative as a **diagnostic-only** field.

**A polymorphic provider object** (`provider.provide(container)` with three
subclasses). Rejected: a class hierarchy for three closed cases, justified only by
extensions (`PROTOTYPE`, `@provides`) that are explicit non-goals.

**A factory receiving the container** (`Callable[[Container], T]`). Rejected: it
hands the container to user code (service locator) and makes every factory pay a
parameter it usually ignores. Dependencies reach a zero-argument factory through the
closure, which is where the composition root already holds them.

**A factory with `Autowired` parameters.** That is `@provides` in disguise: a second
injection engine reading signatures alongside `plans.py`. Non-goal.

**`replace=True` on registration.** Rejected: with real container isolation (spec
1's headline guarantee) a different graph is a different `Container()`. The honest
cost is that classes declared with `@component` land in the default container at
import time, so replacing a single bean for a test means rebuilding the graph by
hand. Revisit only with a concrete case.

**Runtime `isinstance` validation of the binding.** Rejected: `issubclass` cannot
check a structural `Protocol` at all, so the check would be unavailable exactly where
`as_type` is most used and most easily got wrong, while lending false confidence in
the nominal case. The first draft also called it redundant with a static check — that
reason was **withdrawn** when the static check turned out not to exist (point 4); the
argument above stands without it. What remains is that a mis-binding fails loudly at
first use, as an ordinary `AttributeError`.

**Dropping `register_factory` entirely** (`register_instance(create_engine(dsn))`
already works). Considered seriously: a factory adds no capability, only laziness.
Kept for two concrete reasons — an object that must not be built unless something
needs it (a resource that opens connections, a client that wants a running event
loop rather than import time), and rebuild-after-`clear_instances()`. Its
incremental cost is the field the design needed anyway; it is `register_instance`
that is the sugar, not the reverse.

## `origin`: kept knowingly, read by humans only

`origin` has exactly one reader: the dataclass `repr`. No error message consults it —
the "factory returned `None`" message does not need it, since `register_instance(None)`
is rejected at registration, so a `None` there always comes from a real factory.
Dropping it was proposed during the design review and refused deliberately: the field
is executable documentation of the provider model, and it is what makes a definition
legible at a breakpoint. Its cost is one `__slots__` entry and one line every reader
of `definitions.py` must understand.

Consequence for the implementation: the closures behind `register_instance` and the
`repr` must be **named**, not anonymous lambdas, so that a definition inspected in a
debugger reads as `origin=INSTANCE, factory=<pushed_instance ...>` rather than
`factory=<lambda ...>`.

## Implementation order

Six commits, each a valid state of the repository (`uv run pytest` green at every
one), following spec 1's discipline:

1. **Refactor only, no behavior change**: extract `_build_from_class` out of
   `_create`. Tests unchanged and green. Kept separate because this repo's rules
   require refactoring to be separated from feature work.
2. `factory` field, `_origin`, `_build_from_factory`, `_put`, `register_factory`,
   the async rejection, and the defensive guard. The guard ships here rather than
   in a commit of its own: alone it would be an untestable commit, which is
   precisely its own weakness.
3. `register_instance` as sugar over the factory path, plus the `None` rejection.
4. `as_type` on `register` and `register_instance`. After commit 2, not before,
   because it builds on `_put` — leading with it would mean writing the collision
   rule twice.
5. Dual-form decorators, all five aliases.
6. README and `CLAUDE.md`.
