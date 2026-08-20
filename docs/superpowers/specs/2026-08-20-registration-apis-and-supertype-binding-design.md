# Registration APIs: the push primitive and supertype binding

Date: 2026-08-20
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
    def register[T](self, cls: type[T], *, as_type: type[T] | None = None) -> type[T]: ...
    def register_instance[T](self, instance: T, *, as_type: type[T] | None = None) -> None: ...
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

4. **The subtype relation is enforced statically, not at runtime.** With
   `instance: T` / `cls: type[T]` and `as_type: type[T]`, a type checker solves `T`
   to the supertype and verifies assignability — including for structural
   `Protocol`s, which `issubclass()` cannot check at all unless they are
   `@runtime_checkable` and non-data. Adding a best-effort `isinstance` check would
   therefore be non-uniform (enforced for ABCs, silent for Protocols) without
   catching more real errors.

5. **Only `None` is rejected at runtime.** `register_instance(None)` raises
   `RegistrationError`; a factory returning `None` raises
   `DependencyResolutionError` at resolve time. `None` is singled out because it is
   the one value that would corrupt the `ready`/`instance` invariant into
   `ready=True, instance=None` — the "silent `None` typed as `T`" failure class this
   redesign exists to eliminate.

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
   worth more. The static check remains on `container.register`.

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
construction. They are included anyway, for the same reason spec 1 bought the `ready`
guard in `_create`: the failure they prevent is a silent `None` typed as `T`, the
one class of failure this library refuses to allow a future edge kind to reintroduce.

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
| factory returns `None` | `DependencyResolutionError` | `The factory registered for 'Engine' returned None.` |
| `resolve(concrete)` after rebinding | `DependencyResolutionError` | existing message, unchanged |
| cycle through a factory | `CircularDependencyError` | existing message, unchanged |
| factory raises | *propagates as-is* | rollback applied, no wrapping |

The last row is a consistency choice: spec 1 does not wrap exceptions raised by user
`__init__` bodies, and a factory is user code on the same footing.

## Testing

New file `tests/test_registration_apis.py`, plus additions to
`tests/test_circular_dependencies.py` (cycles) and `tests/test_components.py`
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

**FastAPI smoke test, scheduled early in the plan.** `fastapi.py` rewrites a
parameter to `annotation=target` plus `Depends(...)`. With `as_type=SomeProtocol`,
`target` is a `Protocol`, and it must be verified that FastAPI/pydantic does not try
to validate it (it should not — a parameter with a `Depends` default is not a
request field — but this is precisely where an assumption is expensive). If it
fails, the story this spec tells about DIP changes, so the plan must find out early
rather than at the end.

## Documentation

- **README**: a composition-root section — the two real drivers, the "who
  constructs the object" rule, and a **"when you do not need push"** subsection: a
  `BaseSettings` with its own `env_prefix` is zero-argument constructible and can
  simply be registered, which already works today. Push is required when
  configuration depends on entry-point input (CLI arguments, a file chosen at
  runtime, values already in memory).
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

**Runtime `isinstance` validation of the binding.** Rejected: unreliable for
`Protocol`s, therefore non-uniform, and redundant with the static check that the
generic signature already provides.

**Dropping `register_factory` entirely** (`register_instance(create_engine(dsn))`
already works). Considered seriously: a factory adds no capability, only laziness.
Kept for two concrete reasons — an object that must not be built unless something
needs it (a resource that opens connections, a client that wants a running event
loop rather than import time), and rebuild-after-`clear_instances()`. Its
incremental cost is the field the design needed anyway; it is `register_instance`
that is the sugar, not the reverse.

## Open question left deliberately

`origin` has exactly one reader today: the dataclass `repr`. The "factory returned
`None`" message does not need it, since `register_instance(None)` is already
rejected at registration, so a `None` there always comes from a real factory. The
field is kept for human debugging and introspection — legitimate, but it should be
described that way and not passed off as necessary. If a second reader does not
appear during implementation, dropping it is the honest move.
