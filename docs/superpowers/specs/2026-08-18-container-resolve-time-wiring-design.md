# Container redesign: resolve-time wiring

Date: 2026-08-18
Status: Approved for implementation planning

This is **spec 1 of 2**. It replaces the class-instrumentation injection mechanism
with resolve-time construction, and fixes the defects that mechanism made
structurally unfixable. Spec 2 (not written yet) builds the "push" primitive
(`register_instance` / `register_factory`) and supertype binding (`as_type=`) on top
of the foundation this spec establishes.

## Motivation

`Container.register(cls)` currently monkey-patches `__new__` and `__init__` on the
registered class (`container.py:182-183`). The patched callables close over the
`Container` instance that installed them. Every defect below follows from that one
decision, and each was reproduced by running code against the library at `0.3.1`:

| Scenario | Observed |
|---|---|
| Same class registered in two `Container()` instances | The second container returns the **first** container's instances (`s1 is s2`) |
| `class Child(RegisteredBase)` then `Child()` | `KeyError: Child` |
| `Plain()` written by hand after `register(Plain)` | Returns the container's singleton |
| Component with `__slots__` | `AttributeError: no attribute '_di_initializing'` |
| `register()` of a class with an unresolvable class-level annotation | `NameError` at registration time |
| Constructor parameter with no default and no `Autowired` | Raw `TypeError` from Python |

The first row is the most consequential: `README.md` advertises "Explicit,
independent containers" and `container.py:16-18` documents each `Container` as an
independent scope. Both are false. Worse, the mechanism makes them *unachievable*:
wiring is triggered by `Cls()`, and `Cls()` has no way to know which container is
asking. Instrumentation from a second registration wraps the first, so the first
container wins everything.

This is not a latent problem. `tests/test_constructor_injection.py:93-99` carries a
seven-line comment documenting the leak as a "pre-existing Container limitation" and
works around it by duplicating two otherwise-identical classes (`AppSettingsWithEnv`
/ `AppSettingsDefault`). And `tests/test_components.py:78` — named
`test_multiple_containers_independent` — is a false-confidence test: it registers
*different* classes in the two containers, so its `instance1 is not instance2`
assertion is true by construction and tests nothing.

**Goal:** make `register()` a pure registration operation that never mutates the
registered class, and make `resolve()` the single site where an instance is
constructed and wired.

## Non-goals

- The "push" primitive (`register_instance`, `register_factory`, `@provides`) and
  supertype binding (`as_type=`). Deferred to spec 2, deliberately: after this
  redesign, `resolve()` is the only construction site, so "the instance already
  exists" / "build it with a factory" / "build it with `cls()`" becomes a single
  branch in a single place. Building the provider on top of the current mechanism
  would mean writing logic this redesign then deletes.
- `@component` and its aliases, and the module-level default container. Unchanged.
- `Autowired[T]`'s form as a pure annotation. It stays an annotation; it does not
  become a descriptor.
- The public signatures of `Container.register`, `Container.resolve`, and
  `Container.get`.

## Chosen approach

`register()` records; `resolve()` constructs. No class mutation, ever.

```text
1.  definition = registry[cls]                 # else DependencyResolutionError
2.  if definition.instance is not None: return # cached, or a field cycle closing
3.  push cls onto the resolution stack
4.  instance = cls.__new__(cls)
5.  definition.instance = instance             # early registration -> field cycles
6.  if definition.plan is None: definition.plan = InjectionPlan.for_class(cls)
7.  for each planned field:  setattr(instance, name, self.resolve(dep))
8.  for each planned ctor param: if dep is on the stack -> CircularDependencyError
                                 else resolve it
9.  cls.__init__(instance, **ctor_kwargs)
10. finally: pop the stack; on any exception, roll back (see below)
```

**Rollback scope.** A failure must not leave a partially-constructed object reachable.
Rolling back only the currently-failing definition is not enough: in a field cycle, B
may already be fully initialized holding a reference to a partial A, so clearing A
alone would leave B in the registry pointing at an object the registry has disowned.
Rollback therefore clears `instance` on **every definition created during the failed
outermost `resolve()` call** — which is well defined because the whole call runs under
one `RLock` and the exception propagates to that outermost frame. Concretely: the
outermost frame records the stack depth on entry, and on exception clears the
`instance` of every type pushed at or below its own depth. A failed resolution leaves
the container exactly as it was before the call.

"In construction" state moves off the instance and into the container:
`Container._resolving: list[type]`. A list, not a set, because the error message
needs the chain. The per-instance `_di_initializing` / `_di_initialized` flags are
deleted — which is what makes `__slots__` components work and stops polluting user
objects.

The injection plan becomes **lazy and cached**: `BeanDefinition` gains
`plan: InjectionPlan | None`, computed on first `resolve()` rather than at
`register()`. This eliminates the `NameError`-at-registration defect (registration
can no longer fail because of an annotation unrelated to injection) and is also more
correct: `Autowired["X"]` where `X` is defined later in the module *requires* late
binding.

### Alternatives considered

- **Keep instrumentation, make it container-aware via a `ContextVar`.** The patched
  `__new__` would read a "current container" set by `resolve()`. Fixes isolation and
  keeps `Cls()` wired, so it is not breaking. Rejected: it leaves the subclass
  `KeyError`, the `__slots__`/frozen incompatibility, the instance flags, and the
  process-wide hijacking of `Cls()` all in place, while *adding* implicit global
  state — and a `Cls()` call outside any `resolve()` still needs an arbitrary
  fallback. New complexity to preserve behavior we judged wrong.
- **Resolve-time wiring plus `Autowired` as an explicit data descriptor**
  (`dep: Autowired[Dep] = autowired()`), resolving on first attribute read. Its one
  real advantage is that it resolves cycles *correctly*: a lazily-read partner is
  always complete. Rejected for now: it forces new mandatory syntax on every field,
  the descriptor needs to know which container to resolve against, and it gives up
  `Autowired`'s most elegant property — being purely an annotation, invisible at
  runtime. Revisit only if cycles become a practical problem.
- **A diagnostic `__getattr__` installed on registered classes**, so reading an
  un-injected `Autowired` field on a hand-built instance raises a clear pywire error
  instead of a bare `AttributeError`. Rejected as YAGNI, and because it reintroduces
  class mutation — much smaller than the current patching, but still a betrayal of
  the principle this redesign establishes. Additive later if it proves necessary.

## Detailed design

### Module layout

`container.py` currently does three jobs in 184 lines: hold the registry, compute
what must be injected, and instrument the class. The third disappears; the second is
extracted, because mixing it with the first is why "what does this class need?"
cannot be tested without a live container.

| Module | Responsibility after the redesign |
|---|---|
| `container.py` | `Container`: registry, `register`/`resolve`/`get`, resolution stack, the 10-step construction sequence. Nothing else. |
| `plans.py` **(new)** | `InjectionPlan`: *what* a class needs. Frozen slotted dataclass with `fields: dict[str, type]` and `ctor_params: dict[str, type]`, built by `InjectionPlan.for_class(cls)`. |
| `markers.py` | `Autowired` and `resolve_autowired_type` keep their shape; only their error behavior changes (see Error handling). |
| `definitions.py` | `BeanDefinition`: `cls`, `instance`, and the new `plan` cache. `Scope` is removed. |
| `exceptions.py` | A hierarchy instead of a single class. |
| `fastapi.py` | Only a context wrap around `AnnotationResolutionError`. |
| `decorators.py` | Untouched. |

`InjectionPlan.for_class` is a classmethod rather than a separate `InjectionPlanner`
class: planning is genuinely stateless and receives no config or injected
dependency, so the project's "class when config is injected" rule does not apply.

The payoff is testability: `InjectionPlan.for_class(SomeClass)` can be asserted on
directly, with no container, no registration, and no resolution. Today, verifying
that an `Autowired["X"]` annotation is read correctly requires building a container,
registering, resolving, and inspecting the finished object.

### Exception hierarchy

```text
PyWireError(Exception)
├── RegistrationError            # duplicate registration (today: bare ValueError)
├── AnnotationResolutionError    # Autowired["Type"] cannot be resolved (today: silence)
└── DependencyResolutionError    # not registered, or not constructible
    └── CircularDependencyError  # constructor cycle, carrying the chain
```

`CircularDependencyError` subclasses `DependencyResolutionError` because it *is* a
resolution failure; callers who want to handle every resolution problem catch the
parent. `DependencyResolutionError` keeps its current name and export, so
`__init__.py` changes only by addition.

### Cycle policy

Cycles are expressible **only** through field injection. A cycle through constructor
parameters fails with the chain.

This is a policy choice, not a technical necessity: because `__new__` runs before
`__init__` and the instance is registered early, the current code *can* close a
constructor cycle, and `tests/test_constructor_injection.py:150-163` passes today.
The failure mode is narrower than "broken" — it only bites when `__init__` *uses* the
injected partner (calls a method, reads an attribute) instead of merely storing it,
and that is not statically detectable.

The rule is chosen on contract grounds: **a constructor argument is, by contract,
something usable immediately** — handing over a half-constructed object lies about
that contract. An injected field is a stored reference, read later, so a
partially-built partner is legitimate there. This also gives a clean escape hatch: an
intentional cycle is expressed by converting that dependency from a constructor
parameter to a field.

The check lives at step 8, locally — not inside `resolve()` — because only code that
is resolving constructor arguments knows it is in that phase. Step 7 has no check,
and that asymmetry alone implements the whole policy without any extra flag.

### Error handling

Every failure names both what failed and who asked for it. The resolution stack makes
the chain free.

**One new rule that kills two defects.** Since `resolve()` passes no arguments, any
`__init__` parameter that is neither a resolvable `Autowired[T]` nor defaulted is
unsatisfiable. Today that surfaces as a bare Python `TypeError`; separately,
`container.py:84-95` silently skips parameters whose annotations fail to resolve, so a
typo in `Autowired["Typo"]` also surfaces as an incomprehensible `TypeError`. One
rule, applied at plan time, closes both:

> A constructor parameter that is not a resolvable `Autowired[T]` **and** has no
> default makes the class not constructible by the container. Explicit error. With a
> default: ignored, which is the correct behavior.

The per-parameter `NameError` fallback stays — a `TYPE_CHECKING`-only annotation on a
*defaulted* parameter must not break anything — but it can no longer hide a broken
`Autowired`.

`resolve_autowired_type` stops collapsing two different outcomes into one value.
Today it returns `None` both for "not an `Autowired` annotation" and for "an
`Autowired["X"]` whose `X` cannot be resolved" (`markers.py:51-55`). After: `None`
only for the first; the second raises `AnnotationResolutionError`, whose message names
the unresolved name and the module it was looked up in — all `markers.py` knows.
`InjectionPlan.for_class` catches it and re-raises the same exception type with the
owning class and field/parameter name prepended, since it is the only caller that
knows them; `fastapi.py` does the same with the endpoint's qualified name and
parameter name. No other call site needs to catch it.

| Situation | Exception | Message |
|---|---|---|
| type not registered | `DependencyResolutionError` | `Cannot resolve 'Repo' required by 'OrderService.repo': not registered in this container. Resolution chain: OrderService -> Repo` |
| constructor cycle | `CircularDependencyError` | `Circular dependency through constructor parameters: A -> B -> A. Convert one of these dependencies to a field to allow the cycle.` |
| unresolvable `Autowired["X"]` | `AnnotationResolutionError` | `Cannot resolve annotation Autowired["Xx"] on 'OrderService.repo': name 'Xx' is not defined in module 'app.services'.` |
| non-defaulted, non-`Autowired` parameter | `DependencyResolutionError` | `Cannot construct 'NeedsArg': parameter 'url' has no default and is not Autowired. Register an instance instead, or give it a default.` |
| frozen dataclass with injected fields | `DependencyResolutionError` | `Cannot inject field 'dep' into frozen 'FrozenSvc': use constructor injection instead.` |
| custom `__new__` requiring arguments | `DependencyResolutionError` | `Cannot construct 'CustomNew': its __new__ requires arguments. Register a pre-built instance instead.` |
| duplicate registration | `RegistrationError` | `Component 'Svc' is already registered in this container.` |

Two of those messages point at `register_instance`, which is spec 2. Accepted: they
are clear errors regardless, and the wording will not have to change once spec 2
lands.

### Thread safety

A single `threading.RLock` guards the whole of `resolve()`. This is not theoretical:
FastAPI runs `def` (synchronous) endpoints in a threadpool, so `container.resolve()`
really is called from concurrent threads. `RLock` rather than `Lock` because
`resolve()` is recursive by construction.

Held at coarse granularity, it also solves a second problem for free: `_resolving` is
a shared list, and with per-type locks the chain would be corrupted across threads.
The cost is that two threads resolving independent graphs serialize — acceptable,
since construction happens once per bean and every later call is a cache read.
Per-type locks would reintroduce deadlock risk precisely on cycles.

### Removed

- `Container._instrument` and everything it installs.
- `Scope` and `BeanDefinition.scope`. `PROTOTYPE` was declared and never
  implemented; the redesign would make it implementable (a branch on
  `definition.scope`), but it is not needed, and reintroducing it later is roughly
  five lines. A two-valued enum where one value is a lie costs more than it is worth.
- The `_di_initializing` / `_di_initialized` instance attributes.

## Data flow

**A — simple graph.** `resolve(OrderService)` -> not cached -> push `[OrderService]`
-> `__new__` -> early register -> plan (field `repo: Autowired[Repo]`) ->
`resolve(Repo)` recurses and completes -> `setattr` -> `OrderService.__init__(svc)`
-> pop. Observationally identical to today.

**B — field cycle, legal.** `resolve(A)` -> push `[A]` -> `__new__` A -> early
register -> field `b` -> `resolve(B)` -> push `[A, B]` -> `__new__` B -> early
register -> field `a` -> `resolve(A)` -> `definition.instance is not None` ->
**returns the partial A at step 2, without touching the stack** -> `B.__init__` ->
pop -> `setattr(a, "b", b)` -> `A.__init__` -> pop. `a.b is b` and `b.a is a` both
hold. The three tests in `test_circular_dependencies.py` pass unchanged.

**C — constructor cycle, fails.** `resolve(A)` -> push `[A]` -> `__new__` A -> early
register -> plan says `ctor_params = {"b": B}` -> `resolve(B)` -> push `[A, B]` ->
`__new__` B -> plan says `ctor_params = {"a": A}` -> **step 8: `A` is on the stack**
-> `CircularDependencyError`, chain `A -> B -> A`. The `finally` clause empties the
stack **and rolls back every instance created during the call** (see Rollback scope),
without which a second `resolve(A)` after the failure would happily hand back the
never-initialized object from the failed attempt.

**D — isolated containers, now real.** `c1.register(Svc)` and `c2.register(Svc)` do
not touch `Svc`: two `BeanDefinition`s in two dicts, two independent instances.

**E — what stops working.** Declared, not discovered later:

| Scenario | Before | After |
|---|---|---|
| `Svc()` written by hand | wired (the container's singleton) | a plain Python object; `Autowired` fields **absent** |
| `Child(RegisteredBase)()` | `KeyError` | works; it is plain Python |
| `__slots__` component, no injected fields | `AttributeError` | works |
| frozen dataclass **with** injected fields | `AttributeError` | explicit `DependencyResolutionError` |
| custom `__new__` with arguments | worked by accident | explicit `DependencyResolutionError` |
| constructor cycle | half-constructed object | `CircularDependencyError` with the chain |

The first row is the real semantic break: a stray `Svc()` yields `AttributeError:
'Svc' object has no attribute 'repo'` far from the cause. Accepted as the honest
model — `new MyService()` is not wired in Spring either — and the diagnostic
`__getattr__` mitigation was explicitly rejected as YAGNI.

**Verified, not assumed.** The `__new__`/`__init__` split was probed against the
constructs the test suite and the target use cases actually use: pydantic v2
`BaseSettings` (defaults applied, `model_dump()` correct), plain dataclasses, and
`__slots__` classes all construct correctly; frozen dataclasses construct but reject
field `setattr` with `FrozenInstanceError`; a custom `__new__` with required arguments
raises `TypeError`. The last two are exactly the cases turned into explicit errors
above.

## Testing

The redesign is not "same tests, new implementation": six behaviors change on purpose
— a hand-written `Cls()` is no longer wired, constructor cycles raise, unresolvable
`Autowired` annotations raise, frozen classes with injected fields raise, a custom
`__new__` with arguments raises, and duplicate registration raises a different
exception type. Tests describing the new semantics are written first (red), then
`container.py` is rewritten. The tests that do *not* change semantics — the majority —
are the safety net: if `test_circular_dependencies.py` or
`test_fastapi_integration.py` break, that is a regression, not an intended change.

### Existing tests

| File | Outcome |
|---|---|
| `test_circular_dependencies.py` (3) | **Unchanged, must pass.** They are the proof that field cycles stay legal. |
| `test_fastapi_integration.py` (12) | Unchanged, plus one new test for `AnnotationResolutionError` context on an endpoint signature. |
| `test_components.py` (8) | 7 unchanged. `test_multiple_containers_independent` rewritten to use **the same class** in both containers, becoming a real test. `test_duplicate_registration_raises_error` moves from `ValueError` to `RegistrationError`. |
| `test_constructor_injection.py` (9) | The cycle test (`:150-163`) becomes `pytest.raises(CircularDependencyError)` with an assertion on the chain. `AppSettingsWithEnv`/`AppSettingsDefault` collapse into one class and the seven-line workaround comment is deleted. |
| `test_container.py` (6) | Unchanged; none asserts on `scope`, so removing `Scope` does not touch them. |
| `test_markers.py` (2) | Unchanged, plus one new test: `Autowired["Missing"]` raises instead of returning `None`. |

### New tests

- **`tests/test_plans.py`** — `InjectionPlan.for_class` in isolation: fields,
  constructor parameters, both coexisting, forward references, an `__init__` inherited
  from a different module, and an unresolvable parameter with and without a default.
- **Isolation** — the same class in two containers yields two distinct instances, and
  their respective dependencies are distinct too.
- **Non-invasiveness** — after `register(Svc)`: `Svc.__new__ is object.__new__`,
  `Svc()` is a plain object without the injected fields, and an unregistered subclass
  instantiates without error. This test encodes the principle, not just its effects.
- **Construction edge cases** — `__slots__` without injected fields (works), frozen
  dataclass with injected fields (explicit error), custom `__new__` with arguments
  (explicit error), non-defaulted non-`Autowired` parameter (explicit error).
- **Rollback** — after a `CircularDependencyError`, a second `resolve(A)` raises the
  same error rather than returning the partial instance from the failed attempt; and
  in a graph where an unrelated bean was fully constructed before the failure, that
  bean is cleared too, so no object built during the failed call survives in the
  registry.
- **Thread safety** — N threads calling `resolve()` on the same never-resolved graph,
  synchronized with a `threading.Barrier` to maximize collision: exactly one instance
  per bean. This is a probabilistic test, not a proof — but without the barrier it
  never collides and is worth nothing.

### Test hygiene

`conftest.py` is empty (a docstring). Tests using `@component` write into the
module-level default container, which is never reset, so they contaminate each other
invisibly and order-dependently. An `autouse` fixture resets the default container
between tests. No public API is added for this; the fixture touches the module's
private attribute, which is legitimate in a test.

## Implementation sequence

Ordered to produce **one point of no return** rather than a diffuse rewrite, and to
keep the final diff readable.

1. **`exceptions.py` — the hierarchy.** Pure addition, nobody uses it yet, suite
   green.
2. **`plans.py` + `tests/test_plans.py` — extraction at unchanged behavior.** The
   logic currently inside `_instrument` moves to `InjectionPlan.for_class`, driven by
   the new tests. **`container.py` keeps using the old path.** Suite green, zero
   behavior change, fully reversible. This ordering is what makes step 4 readable: the
   diff on `container.py` becomes *a deletion* plus a short `resolve()`, instead of a
   simultaneous move-and-rewrite where refactoring cannot be told apart from semantic
   change.
3. **`markers.py` — stop swallowing.** `resolve_autowired_type` raises
   `AnnotationResolutionError` on unresolvable forward references. Pulls in the
   context wrap in `plans.py` and `fastapi.py`, plus one new test in
   `test_markers.py`.
4. **Point of no return — `container.py` + `definitions.py`.** `register()` stops
   instrumenting; `resolve()` becomes the 10 steps; `_instrument` is deleted;
   `BeanDefinition` gains `plan` and loses `scope`; `Scope` is deleted; the `RLock`
   and the `_resolving` stack with `finally` rollback arrive. **Not divisible**: the
   three behaviors change in the same commit, because the old and new mechanisms
   cannot coexist on the same class. The suite goes red here on purpose.
5. **Semantics-changing tests + `conftest.py`.** The rewrites and additions listed
   above. The suite goes green again: *this* is where the work is done, not step 4.
6. **`fastapi.py`.** Only the `AnnotationResolutionError` context wrap. All 12
   existing tests must pass unchanged; a break is a regression.
7. **Documentation.** `README.md`: drop the false "Explicit, independent containers"
   claim *and* add the line that now makes it true; document that a hand-written
   `Cls()` is not wired. `CLAUDE.md`: the "Field injection mechanism" section
   describes the monkey-patching in ~15 lines and is rewritten from scratch, together
   with the module table (`plans.py`) and the `Scope` line.

Not touched: `decorators.py`, and the public signatures of `Container.register`,
`resolve`, and `get`.

## Out of scope

Deferred to spec 2: `register_instance` / `register_factory`, `as_type=`,
`@provides`.

Declared YAGNI: the diagnostic `__getattr__`, `Autowired` as a descriptor,
`PROTOTYPE`, qualifiers, per-router container overrides.

## Migration notes (breaking change)

Target version `0.4.0`. `scripts/bump-version.sh minor` is **not** run as part of
implementation — per project rule it runs only on explicit request, once
implementation is complete.

The library has a single consumer (its author), so no deprecation period is provided.
The one behavioral change a caller can trip over is that a hand-written `Cls()` on a
registered component is no longer wired: every such call site must go through
`container.resolve(Cls)` (or `container.get(Cls)`) instead. Anything already resolving
through the container needs no change.
