# Container redesign: resolve-time wiring

Date: 2026-08-18 (revised 2026-08-19 after design review)
Status: Approved for implementation

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
| `Autowired` field declared on a base class | Silently **not** injected into the subclass |

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

The public `resolve()` delegates to an internal `_resolve(target, edge)`; the extra
argument is how *edge kinds* are threaded through the recursion without touching the
public signature.

```text
resolve(t)  ->  _resolve(t, CTOR if self._resolving else ROOT)

_resolve(t, edge), entirely under the container's RLock:
 1. definition = registry[t]                     # else DependencyResolutionError
 2. if t is already on the resolution stack:      # a cycle is closing
        if any edge from t's position forward, plus `edge`, is CTOR:
            -> CircularDependencyError with the chain
        return definition.instance                # legal field cycle: partial partner
    if definition.instance is not None:
        return definition.instance                # ordinary cache hit
 3. created_mark = len(self._created); push (t, edge) onto the resolution stack
 4. if definition.plan is None: definition.plan = InjectionPlan.for_class(t)
 5. instance = t.__new__(t)
 6. definition.instance = instance; self._created.append(definition)
 7. for each planned field:      setattr(instance, name, _resolve(dep, FIELD))
 8. for each planned ctor param: kwargs[name] = _resolve(dep, CTOR)
 9. t.__init__(instance, **kwargs)
10. on exception: attach context once, then truncate self._created back to
    created_mark, clearing `instance` on every definition dropped
11. finally: pop the resolution stack; if it is now empty, clear self._created
```

Three things about this ordering are deliberate and easy to get wrong:

- **Planning precedes allocation** (step 4 before step 5). A class that cannot be
  planned is never allocated and never early-registered, which shrinks the rollback
  surface to graphs that actually started building.
- **Fields precede `__init__`** (step 7 before step 9). This is a *stated contract*,
  not an accident of the implementation: by the time a component's `__init__` body
  runs, its injected fields are set and readable. It is the only reason the
  `__new__`/`__init__` split is mandatory rather than plain `cls(**kwargs)`. It gets
  its own test, because nothing in the suite currently exercises it and a silent
  reordering would otherwise go unnoticed.
- **The cycle check lives at step 2, not step 8.** See Cycle policy.

**Container state.** `_registry: dict[type, BeanDefinition]`;
`_resolving: list[tuple[type, _EdgeKind]]` — the chain, plus how each frame was
entered; `_created: list[BeanDefinition]` — the undo log; `_lock: threading.RLock`.
Two lists with one job each, rather than one list with two: `_resolving` answers
"where am I and how did I get here", `_created` answers "what must I undo".

The per-instance `_di_initializing` / `_di_initialized` flags are deleted — which is
what makes `__slots__` components work and stops polluting user objects.

**Rollback scope.** A failure must not leave a partially-constructed object
reachable. Rolling back only the currently-failing definition is not enough: in a
field cycle, B may already be fully initialized holding a reference to a partial A,
so clearing A alone would leave B in the registry pointing at an object the registry
has disowned.

Rollback is therefore **per-subtree**: every `_resolve` frame records
`len(self._created)` on entry and, on exception, truncates `_created` back to that
mark, clearing `instance` on every definition it drops. The outermost frame's subtree
is the whole call, so the field-cycle scenario above is covered — B was created inside
A's subtree and is dropped with it.

Per-subtree rather than outermost-only matters for one reachable case:

```python
def __init__(self) -> None:
    try:
        self.metrics = container.resolve(MetricsGraph)
    except DependencyResolutionError:
        self.metrics = None
```

If `MetricsGraph` partially builds and then fails, the exception is **caught** and
never reaches the outermost frame. With outermost-only rollback, `_created` would be
discarded on the *outer* call's success and every partial object built during the
failed inner attempt would stay cached forever — precisely the disaster rollback
exists to prevent, reached through entirely ordinary user code. Per-subtree rollback
fires at the failing frame, before anyone can catch it.

`definition.plan` is **not** cleared by rollback. A plan is a pure function of the
class; a construction failure says nothing about its validity.

**Lazy planning.** `BeanDefinition` gains `plan: InjectionPlan | None`, computed on
first `resolve()` rather than at `register()`. Registration can no longer fail because
of an annotation unrelated to injection, and `Autowired["X"]` where `X` is defined
later in the module *requires* this late binding.

The plan cache stays on `BeanDefinition`, so the same class registered in two
containers is planned twice. A module-level `WeakKeyDictionary[type, InjectionPlan]`
would memoize it globally and was considered and rejected: it reintroduces
process-wide mutable state into a redesign whose entire thesis is "no global side
effects", to save microseconds on a once-per-class-per-container operation.

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
- **Construct with `cls(**ctor_kwargs)` and inject fields afterwards.** Much less
  invasive: no `__new__`/`__init__` split, works with custom `__new__`, frozen
  classes and exotic metaclasses. Rejected because it breaks the "fields are set
  before `__init__` runs" contract above, and because a field cycle would then need a
  deferred second pass.
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
| `container.py` | `Container`: registry, `register`/`resolve`/`get`, resolution stack with edge kinds, the construction sequence, rollback, the lock. Nothing else. |
| `plans.py` **(new)** | `InjectionPlan`: *what* a class needs. Frozen slotted dataclass with `fields: dict[str, type]` and `ctor_params: dict[str, type]`, built by `InjectionPlan.for_class(cls)`, which also rejects classes the container cannot construct. |
| `markers.py` | `Autowired`, `resolve_autowired_type` (now raising), and `is_autowired_annotation_text` for classifying annotations that cannot be evaluated. |
| `definitions.py` | `BeanDefinition`: `cls`, `instance`, and the new `plan` cache. `Scope` is removed. |
| `exceptions.py` | A hierarchy instead of a single class, with composable context. |
| `fastapi.py` | Only passes endpoint context to `resolve_autowired_type`. |
| `decorators.py` | Untouched. |

`InjectionPlan.for_class` is a classmethod rather than a separate `InjectionPlanner`
class: planning is genuinely stateless and receives no config or injected
dependency, so the project's "class when config is injected" rule does not apply.

The payoff is testability: `InjectionPlan.for_class(SomeClass)` can be asserted on
directly, with no container, no registration, and no resolution. Today, verifying
that an `Autowired["X"]` annotation is read correctly requires building a container,
registering, resolving, and inspecting the finished object.

### What `InjectionPlan.for_class` does

In order, because the order is load-bearing:

1. **Reject an unconstructible `__new__`.** `inspect.signature(cls.__new__)`: the
   class is constructible iff every parameter after `cls` has a default or is
   variadic. Checked statically rather than by catching `TypeError` around
   `cls.__new__(cls)` — a `TypeError` raised *inside* a legitimate user `__new__`
   would otherwise be reported as "its `__new__` requires arguments", which is
   actively misleading. If the signature cannot be introspected at all, the class is
   assumed constructible and construction is left to speak for itself.
2. **Plan constructor parameters.** `get_type_hints(original_init)` with **no explicit
   `globalns`** — `original_init` may be inherited from a base defined in another
   module, so `cls.__module__` would be the wrong resolution context, whereas
   `get_type_hints` reads `original_init.__globals__` internally. Intersected with
   `inspect.signature(original_init).parameters`, skipping `self` and variadics. A
   forward reference *inside* `Autowired["X"]` is likewise evaluated against
   `original_init.__globals__`, not the subclass's module. A parameter that is
   neither a resolvable `Autowired[T]` nor defaulted raises.
3. **Plan fields, walking the MRO.** See below.
4. **Deduplicate: a name present in `ctor_params` is removed from `fields`.** See
   below.
5. **Reject un-settable fields**: a frozen dataclass with injected fields *remaining
   after step 4*.

Steps 1, 2 and 5 raise `DependencyResolutionError`; step 3 raises
`AnnotationResolutionError`. The returned `InjectionPlan` carries **no failure
state** — it is purely `fields` + `ctor_params`, which is what a frozen slotted
dataclass should be. `resolve()` adds no checks of its own; the exception simply
propagates from step 4 of the construction sequence, where rollback already covers it.

### Field planning walks the MRO

`inspect.get_annotations(cls)` returns the class's **own** annotations only — verified,
no MRO walk. Under the current mechanism that means an `Autowired` field declared on a
base class is silently not injected into a subclass. The redesign makes subclassing a
first-class scenario for the first time (`Child(RegisteredBase)()` moves from `KeyError`
to "works"), so leaving that silence in place is no longer acceptable.

`_plan_fields` therefore walks `reversed(cls.__mro__)`, skipping `object`, and resolves
each annotation against **its own defining class's module globals** — not `cls`'s. Using
`cls.__module__` for an inherited annotation is exactly the mistake `container.py:67-72`
already documents avoiding on the constructor path.

Because the walk runs base-first, a subclass annotation overrides the base's. That makes
**re-annotating a field without `Autowired` the supported way to opt out** of an
inherited injection:

```python
class Base:
    repo: Autowired[Repo]

class Child(Base):
    repo: Repo          # not injected; Child supplies it some other way
```

The alternative — unioning every `Autowired` found anywhere in the MRO — would make
opting out impossible. This is documented behavior with a test, not an accident.

### Constructor parameters win over fields

Verified: `@dataclass class Svc: repo: Autowired[Repo]` puts `repo` in **both**
`__annotations__` *and* `__init__`'s parameters, frozen or not. Without dedup the
container would `setattr` it at step 7 and then resolve it again at step 8 and pass it
to `__init__`, which assigns it a third time — harmless but incoherent.

The frozen case is not harmless. A `@dataclass(frozen=True)` component with an
`Autowired` field would record a non-empty `fields`, trip the frozen check, and raise
*"use constructor injection instead"* — at a user who did exactly that. Only the
double-counting made the class look unconstructible.

**Rule: a name present in `ctor_params` is removed from `fields`.** One line, and it
fixes the double resolution and the false positive together. A frozen dataclass then
ends up with empty `fields` and constructs cleanly, and the frozen check only fires for
a genuinely frozen class with an injected field that is *not* a constructor parameter —
the case its message actually describes.

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
parent.

`PyWireError` carries structured context and composes its own message:

```python
class PyWireError(Exception):
    def __init__(self, message: str, *, chain: tuple[type, ...] = ()) -> None: ...
    requester: str | None    # "OrderService.repo", set by the frame that knows it
    chain: tuple[type, ...]  # the resolution chain, set where the error is raised
    def __str__(self) -> str: ...  # message + requester + chain, composed once
```

This is what makes "contextualize exactly once" cheap — see Error handling.

### Cycle policy

**A cycle containing at least one constructor edge is illegal, whichever type you
resolve first. A cycle through fields only is legal.**

The justification is mechanical, not contractual. An earlier draft argued from
contract — "a constructor argument is by contract usable immediately, so handing over
a half-built object lies about that" — but that argument refutes itself: in a legal
field cycle, the partner's `__init__` *also* observes a partially-wired object,
because fields are injected before `__init__` runs. The two cases are not
distinguished by what the callee may assume. They are distinguished by whether a fixed
point exists at all:

> A constructor parameter must be *resolved before* `__init__` can be called, so a
> cycle through constructor parameters has no fixed point. A field can be assigned to
> an already-allocated object, so a cycle through fields does.

**The check is cycle-shaped, not traversal-shaped.** The naive implementation — check
`dep in self._resolving` when resolving a constructor argument — makes mixed cycles
succeed or fail depending on the entry point. With `A.__init__(b: Autowired[B])` and
`B.a: Autowired[A]`:

- `resolve(A)` reaches the cycle through B's *field* edge, hits the cache, and
  **succeeds** — handing `B.__init__` the half-built `A` this policy exists to forbid.
- `resolve(B)` reaches it through A's *constructor* edge and **fails**.

Same graph, same container, opposite outcomes, decided by whichever type the
application happens to ask for first — which in a FastAPI app is decided by request
routing.

So the check moves to step 2, where the cycle is *closing*, and inspects the cycle
rather than the current frame: when the requested type is found on `_resolving`, scan
the edge kinds from that type's position forward, **plus the incoming edge** (the edge
closing the cycle is not on the stack yet). If any is a constructor edge, raise.

This is deterministic and entry-point independent, it eliminates the last path by which
a constructor can receive a partial object, and it makes step 8 need no check at all.
It costs one extra list and one short scan, taken only when a cycle actually closes.

**The escape hatch is unchanged**: an intentional cycle is expressed by converting the
dependency from a constructor parameter to a field.

**The remaining sharp edge, stated plainly**: in a legal field cycle, the partner's
`__init__` observes a partially-wired object. Storing the reference is fine; calling
methods on it during `__init__` is not. This goes in `README.md`, because it is the
one thing left that a user can cut themselves on.

A `resolve()` called by hand from inside a component's `__init__` is recorded as a
**constructor edge**, not a root: it genuinely happens during construction, and
whatever it returns is used immediately. Recording it as a root would open a hole in
this policy.

### Error handling

Every failure names what failed, who asked for it, and the chain. The resolution stack
makes the chain free.

**One new rule that kills two defects.** Since `resolve()` passes no arguments, any
`__init__` parameter that is neither a resolvable `Autowired[T]` nor defaulted is
unsatisfiable. Today that surfaces as a bare Python `TypeError`; separately,
`container.py:84-95` silently skips parameters whose annotations fail to resolve, so a
typo in `Autowired["Typo"]` also surfaces as an incomprehensible `TypeError`. One
rule, applied at plan time, closes both:

> A constructor parameter that is not a resolvable `Autowired[T]` **and** has no
> default makes the class not constructible by the container. Explicit error. With a
> default: ignored, which is the correct behavior.

**Laziness relocates the `NameError` defect; it does not eliminate it.** An earlier
draft claimed lazy planning fixed the "unresolvable class-level annotation aborts
registration" bug. It only moves it from registration to resolution, which is arguably
worse: registration failure is loud at import, resolution failure happens at request
time. The constructor path already has a per-parameter fallback for exactly this
(`container.py:84-95`); the field path has none. The fix is to mirror it: fetch raw
annotations and evaluate **each one individually**, skipping the ones that fail *and*
are not `Autowired`.

The naive form of that fix is wrong. Under `from __future__ import annotations` an
`Autowired[Dep]` field arrives as the string `"Autowired[Dep]"`, so simply not
evaluating would stop injection working everywhere. And once evaluation has failed with
`NameError`, `get_origin` cannot classify what it was — the classification has to work
on unevaluated text.

Textual prefix matching (`ann.startswith("Autowired[")`) fails on
`pywire.Autowired[X]`, on `from pywire import Autowired as Wired`, and on any
`TYPE_CHECKING` alias — all normal ways to import. So: **parse, don't pattern-match.**
`ast.parse(ann, mode="eval").body`; if it is an `ast.Subscript`, evaluate *only*
`node.value` — the base expression — against the module globals. If that yields the
`Autowired` alias, the annotation was an `Autowired` whose argument is broken and
`AnnotationResolutionError` is raised. Otherwise it is skipped silently. This handles
dotted access and aliasing because it evaluates the real base expression instead of
guessing at its spelling. It lives in `markers.py` as `is_autowired_annotation_text`,
next to `resolve_autowired_type` — "is this text an `Autowired` annotation" is marker
knowledge.

`resolve_autowired_type` stops collapsing two different outcomes into one value.
Today it returns `None` both for "not an `Autowired` annotation" and for "an
`Autowired["X"]` whose `X` cannot be resolved" (`markers.py:51-55`). After: `None`
only for the first; the second raises `AnnotationResolutionError`. It gains an optional
`context` argument so the caller that knows the owner — `InjectionPlan.for_class` for a
class member, `fastapi.py` for an endpoint parameter — can name it in the message. No
other call site needs to catch anything.

**Context is attached exactly once, at the deepest frame that knows the member name.**
Every frame on the way out would otherwise prepend its own, so a four-deep graph would
read `App.svc: Svc.repo: Repo.conn: Cannot resolve 'Conn'` — the chain spelled twice,
once as prose and once as the chain string. The frame that catches sets
`err.requester` only if it is still `None`; outer frames see it set and re-raise
untouched. The chain is read from `_resolving` where the error is raised, which is
where it is complete, and stored as a real attribute so tests assert on structure
rather than regexing the message.

| Situation | Exception | Message |
|---|---|---|
| type not registered | `DependencyResolutionError` | `Cannot resolve 'Repo': it is not registered in this container. Required by 'OrderService.repo'. Resolution chain: OrderService -> Repo` |
| cycle with a constructor edge | `CircularDependencyError` | `Circular dependency through a constructor parameter: A -> B -> A. Convert one of these dependencies to a field to allow the cycle.` |
| unresolvable `Autowired["X"]` | `AnnotationResolutionError` | `Cannot resolve annotation Autowired["Xx"] on 'OrderService.repo': name 'Xx' is not defined in module 'app.services'.` |
| non-defaulted, non-`Autowired` parameter | `DependencyResolutionError` | `Cannot construct 'NeedsArg': parameter 'url' has no default and is not Autowired. Register an instance instead, or give it a default.` |
| frozen dataclass with injected fields | `DependencyResolutionError` | `Cannot inject field 'dep' into frozen 'FrozenSvc': use constructor injection instead.` |
| field that cannot be set (e.g. `__slots__`) | `DependencyResolutionError` | `Cannot inject field 'dep' into 'SlottedSvc': the attribute cannot be set. Use constructor injection instead.` |
| custom `__new__` requiring arguments | `DependencyResolutionError` | `Cannot construct 'CustomNew': its __new__ requires argument 'token'. Register a pre-built instance instead.` |
| duplicate registration | `RegistrationError` | `Component 'Svc' is already registered in this container.` |

Note the word order in row 1: because `PyWireError.__str__` composes
`message + requester + chain`, the requester follows the failure rather than being
spliced into it. Same information, one composition site, no `args` rewriting.

Two of those messages point at `register_instance`, which is spec 2. Accepted: they
are clear errors regardless, and the wording will not have to change once spec 2 lands.

### Thread safety

A single `threading.RLock` guards the whole of `resolve()`. This is not theoretical:
FastAPI runs `def` (synchronous) endpoints in a threadpool, so `container.resolve()`
really is called from concurrent threads. `RLock` rather than `Lock` because
`resolve()` is recursive by construction.

Held at coarse granularity, it also solves a second problem for free: `_resolving` and
`_created` are shared lists, and with per-type locks both would be corrupted across
threads. The cost is that two threads resolving independent graphs serialize —
acceptable, since construction happens once per bean. Per-type locks would reintroduce
deadlock risk precisely on cycles.

Three consequences, documented rather than discovered:

- **Every cache hit takes the lock, forever.** The steady-state hot path — every
  `Depends` resolution on every request — acquires the `RLock`. The standard fix, a
  lock-free `definition.instance is not None` read before acquiring, is **unsafe here**:
  step 6 publishes the instance *before* fields and `__init__` have run, so a second
  thread taking that path would observe a genuinely half-built object and hand it to
  application code. The early publication that makes field cycles work is exactly what
  forbids the fast path. An uncontended `RLock` acquire is ~100ns against a request
  measured in milliseconds; if profiling ever demands it, the correct fix is a separate
  `published` flag set after step 9, not a bare `instance` read.
- **User `__init__` code runs under the lock.** A component whose `__init__` opens a
  connection blocks every other thread's `resolve()` for its duration — tolerable at
  startup. But a component `__init__` that waits on work performed by *another* thread
  which itself calls `resolve()` will deadlock: `RLock` is reentrant per-thread, not
  cross-thread.
- **Rollback restores the registry, not the world.** A failed resolution leaves the
  *container* exactly as it was, but cannot undo side effects in `__init__` bodies that
  already ran, and a retry re-runs them.

### Removed

- `Container._instrument` and everything it installs.
- `Scope` and `BeanDefinition.scope`. `PROTOTYPE` was declared and never
  implemented; the redesign would make it implementable (a branch on
  `definition.scope`), but it is not needed, and reintroducing it later is roughly
  five lines. A two-valued enum where one value is a lie costs more than it is worth.
- The `_di_initializing` / `_di_initialized` instance attributes.
- `BeanDefinition` from `pywire.__all__` — see below.

### Public surface

`__init__.py` gains `PyWireError`, `RegistrationError`, `AnnotationResolutionError`
and `CircularDependencyError`: callers need to catch them.

It does **not** gain `InjectionPlan` — an implementation detail; `tests/test_plans.py`
imports it from `pywire.plans` directly.

It **loses** `BeanDefinition`. It is internal machinery with a mutable `instance`, this
redesign grows it further with `plan`, and it exposes exactly the guts the redesign
exists to encapsulate. `test_container.py:95-97` already reaches through
`container._registry` rather than importing it, which suggests the export was never
load-bearing. `0.4.0` is already a breaking release with a single consumer, so this is
the free moment. `Scope` was never exported, so deleting it breaks nothing.

## Data flow

**A — simple graph.** `resolve(OrderService)` -> not cached -> push
`[(OrderService, ROOT)]` -> plan -> `__new__` -> early register -> field
`repo: Autowired[Repo]` -> `_resolve(Repo, FIELD)` recurses and completes -> `setattr`
-> `OrderService.__init__(svc)` -> pop. Observationally identical to today.

**B — field cycle, legal.** `resolve(A)` -> push `[(A, ROOT)]` -> plan -> `__new__` A
-> early register -> field `b` -> `_resolve(B, FIELD)` -> push `[(A,ROOT), (B,FIELD)]`
-> `__new__` B -> early register -> field `a` -> `_resolve(A, FIELD)` -> **A is on the
stack**; edges from A's position are `[ROOT, FIELD]` plus incoming `FIELD` — no
constructor edge -> **returns the partial A, without pushing** -> `B.__init__` -> pop
-> `setattr(a, "b", b)` -> `A.__init__` -> pop. `a.b is b` and `b.a is a` both hold.
The three tests in `test_circular_dependencies.py` pass unchanged.

**C — constructor cycle, fails.** `resolve(A)` -> push `[(A,ROOT)]` -> plan says
`ctor_params = {"b": B}` -> `_resolve(B, CTOR)` -> push `[(A,ROOT), (B,CTOR)]` -> plan
says `ctor_params = {"a": A}` -> `_resolve(A, CTOR)` -> **A is on the stack**; edges
`[ROOT, CTOR]` plus incoming `CTOR` -> `CircularDependencyError`, chain `A -> B -> A`.
Rollback drops every instance created during the failed subtree, without which a second
`resolve(A)` would hand back the never-initialized object from the failed attempt.

**D — mixed cycle, fails from either end.** `A.__init__(b: Autowired[B])` and
`B.a: Autowired[A]`. From `resolve(A)`: the cycle closes on B's *field* edge, but the
scan from A's position sees `[ROOT, CTOR]` and rejects. From `resolve(B)`: the cycle
closes on A's *constructor* edge and the scan sees `[ROOT, FIELD]` plus incoming
`CTOR` and rejects. Identical outcome from both entry points — which is the whole point
of checking the cycle instead of the frame.

**E — isolated containers, now real.** `c1.register(Svc)` and `c2.register(Svc)` do
not touch `Svc`: two `BeanDefinition`s in two dicts, two independent instances.

**F — what stops working.** Declared, not discovered later:

| Scenario | Before | After |
|---|---|---|
| `Svc()` written by hand | wired (the container's singleton) | a plain Python object; `Autowired` fields **absent** |
| `Child(RegisteredBase)()` | `KeyError` | works; it is plain Python |
| `Autowired` field on a base class | silently not injected | injected; re-annotate to opt out |
| `__slots__` component, no injected fields | `AttributeError` | works |
| frozen dataclass **with** injected fields | `AttributeError` | explicit `DependencyResolutionError` |
| frozen dataclass with `Autowired` **dataclass** fields | `AttributeError` | works (constructor injection) |
| custom `__new__` with arguments | worked by accident | explicit `DependencyResolutionError` |
| constructor cycle | half-constructed object | `CircularDependencyError` with the chain |
| mixed field/constructor cycle | half-constructed object, or worked | `CircularDependencyError`, either entry point |
| duplicate registration | `ValueError` | `RegistrationError` |
| `from pywire import BeanDefinition` | worked | `ImportError`; import from `pywire.definitions` |

The first row is the real semantic break: a stray `Svc()` yields `AttributeError:
'Svc' object has no attribute 'repo'` far from the cause. Accepted as the honest
model — `new MyService()` is not wired in Spring either — and the diagnostic
`__getattr__` mitigation was explicitly rejected as YAGNI.

**Verified, not assumed.** The `__new__`/`__init__` split was probed against the
constructs the test suite and the target use cases actually use: pydantic v2
`BaseSettings` (defaults applied, `model_dump()` correct), plain dataclasses, and
`__slots__` classes all construct correctly; frozen dataclasses construct but reject
field `setattr` with `FrozenInstanceError`; a custom `__new__` with required arguments
raises `TypeError`. Separately verified for this revision: `inspect.get_annotations`
performs no MRO walk, and a dataclass `Autowired` field appears in `__annotations__`
and `__init__`'s parameters simultaneously.

## Testing

The redesign is not "same tests, new implementation": behaviors change on purpose —
a hand-written `Cls()` is no longer wired, cycles with a constructor edge raise,
unresolvable `Autowired` annotations raise, frozen classes with injected fields raise,
a custom `__new__` with arguments raises, inherited fields are now injected, and
duplicate registration raises a different exception type. Tests describing the new
semantics are written first (red), then `container.py` is rewritten. The tests that do
*not* change semantics — the majority — are the safety net: if
`test_circular_dependencies.py` or `test_fastapi_integration.py` break, that is a
regression, not an intended change.

### Existing tests

| File | Outcome |
|---|---|
| `test_circular_dependencies.py` (3) | **Unchanged, must pass.** They are the proof that field cycles stay legal. |
| `test_fastapi_integration.py` (12) | Unchanged, plus one new test for `AnnotationResolutionError` context on an endpoint signature. |
| `test_components.py` (8) | 7 unchanged. `test_multiple_containers_independent` rewritten to use **the same class** in both containers, becoming a real test. `test_duplicate_registration_raises_error` moves from `ValueError` to `RegistrationError`. |
| `test_constructor_injection.py` (9) | The cycle test (`:150-163`) becomes `pytest.raises(CircularDependencyError)` with an assertion on the chain. `AppSettingsWithEnv`/`AppSettingsDefault` collapse into one class and the seven-line workaround comment is deleted. |
| `test_container.py` (6) | Unchanged; none asserts on `scope`, and `test_bean_definition_metadata` reaches through `container._registry` rather than importing `BeanDefinition`, so unexporting it does not touch them. |
| `test_markers.py` (2) | Unchanged, plus tests that `Autowired["Missing"]` raises and that a non-`Autowired` annotation still returns `None`. |

### New tests

- **`tests/test_plans.py`** — `InjectionPlan.for_class` in isolation: fields,
  constructor parameters, both coexisting, forward references, an `__init__` inherited
  from a different module, **a field inherited from a base in a different module**, **a
  subclass re-annotation cancelling an inherited injection**, **a dataclass and a frozen
  dataclass with `Autowired` fields**, an unrelated unevaluable annotation not breaking
  planning, and a broken `Autowired` naming its owner.
- **Isolation** — the same class in two containers yields two distinct instances, and
  their respective dependencies are distinct too.
- **Non-invasiveness** — after `register(Svc)`: `Svc.__new__` and `Svc.__init__` are
  unchanged, `Svc()` is a plain object without the injected fields, and an unregistered
  subclass instantiates without error. This test encodes the principle, not just its
  effects.
- **The `__init__`-sees-fields contract** — a component whose `__init__` reads an
  injected field. Nothing currently exercises this, so a silent reordering of the
  construction sequence would otherwise go unnoticed.
- **Construction edge cases** — `__slots__` without injected fields (works), frozen
  dataclass with injected fields (explicit error), custom `__new__` with arguments
  (explicit error), non-defaulted non-`Autowired` parameter (explicit error).
- **Cycles** — the mixed field/constructor cycle rejected from **both** entry points;
  the pure field cycle still legal.
- **Rollback** — after a `CircularDependencyError`, a second `resolve(A)` raises the
  same error rather than returning the partial instance from the failed attempt; in a
  graph where an unrelated bean was fully constructed before the failure, that bean is
  cleared too; and **a failure inside a `resolve()` called from a component's `__init__`
  and caught there leaves no partial instance cached** — the per-subtree rollback case.
- **Thread safety** — N threads calling `resolve()` on the same never-resolved graph,
  synchronized with a `threading.Barrier` to maximize collision: exactly one instance
  per bean. This is a probabilistic test, not a proof — but without the barrier it
  never collides and is worth nothing.

### Test hygiene

`conftest.py` is empty (a docstring). Tests using `@component` write into the
module-level default container, which is never reset, so they contaminate each other
invisibly and order-dependently.

The obvious fixture — `decorators._default_container = None` between tests — **breaks
the suite**. `tests/test_fastapi_integration.py:225` decorates `DefaultContainerRepo`
at *import* time; replacing the container object destroys that registration after the
first test, and the module is never re-imported. The failure would read as a redesign
regression rather than a fixture bug.

The fixture therefore **snapshots and restores** instead: record the registered classes
before the test, and on teardown rebuild `_registry` as fresh `BeanDefinition`s for
exactly those classes. Import-time registrations survive; per-test registrations and
every cached instance are discarded. No public API is added; the fixture touches the
module's private attribute, which is legitimate in a test.

## Implementation sequence

Ordered to produce **one point of no return** rather than a diffuse rewrite, and to
keep the final diff readable.

1. **`exceptions.py` — the hierarchy**, with `requester`/`chain` and the composing
   `__str__`. Plus the `__init__.py` export changes. Pure addition apart from
   unexporting `BeanDefinition`; suite green.
2. **`plans.py` — verbatim extraction, and `container.py` switched onto it.** The logic
   currently inside `_instrument` moves to `InjectionPlan.for_class` **at unchanged
   semantics** — own annotations only, existing fallback shape — and `_instrument`
   starts calling it. The existing green suite is then a real equivalence proof, and
   the step is fully reversible. Splitting this from step 3 is what makes step 5
   readable: the diff on `container.py` becomes *a deletion* plus a short `resolve()`,
   instead of a simultaneous move-and-rewrite where refactoring cannot be told apart
   from semantic change.
3. **`markers.py` — stop swallowing.** `resolve_autowired_type` gains `context` and
   raises `AnnotationResolutionError`; `is_autowired_annotation_text` arrives. Pulls in
   the context arguments in `plans.py` and `fastapi.py`, plus new tests in
   `test_markers.py`.
4. **`plans.py` — the real planner.** MRO walk with per-owner globals, per-annotation
   evaluation with the AST classifier, constructor/field dedup, and the three
   constructibility rejections. Driven by `test_plans.py`; `container.py` still on the
   old mechanism. The suite stays green — verified: no existing test has an inherited
   `Autowired` field, an unevaluable field annotation, a dataclass component, or a
   non-defaulted non-`Autowired` parameter. Transiently, because the old `_instrument`
   plans at *registration* time, these rejections fire at registration until step 5
   makes planning lazy; that is fine and invisible to the suite.
5. **Point of no return — `container.py` + `definitions.py`.** `register()` stops
   instrumenting; `resolve()` becomes the construction sequence; `_instrument` is
   deleted; `BeanDefinition` gains `plan` and loses `scope`; `Scope` is deleted; the
   `RLock`, the edge-kind resolution stack, `_created` and per-subtree rollback arrive.
   **Not divisible**: the behaviors change in the same commit, because the old and new
   mechanisms cannot coexist on the same class. The suite goes red here on purpose.
6. **Semantics-changing tests + `conftest.py`.** The rewrites and additions listed
   above. The suite goes green again: *this* is where the work is done, not step 5.
7. **Documentation.** See below.

Not touched: `decorators.py`, and the public signatures of `Container.register`,
`resolve`, and `get`.

### Documentation scope

Larger than "drop one claim and add one line". Grounded in the current files:

`README.md` — line 31's "Explicit, independent containers" replaced with the claim that
is now true; **line 32's `BeanDefinition` feature bullet deleted** (it is no longer
exported); **lines 81-84 deleted entirely** — that paragraph does not merely describe
the old manual-construction behavior, it *sells* it, and it is the most misleading
thing that would otherwise survive; line 30's "Circular dependency detection and
handling" qualified to field-only; the architecture tree gaining `plans.py`. Plus new
material: the exception hierarchy, inherited-field injection and the re-annotation
opt-out, the "fields are set before `__init__` runs" contract stated for fields as
line 69 already states it for constructors, and the caveat that a legal field cycle
exposes a partial partner. Line 166 already labels `exceptions.py` as "Exception
hierarchy" — that becomes true rather than aspirational. Line 3's "Python 3.12+"
disagrees with `CLAUDE.md`'s 3.13; fix while in there.

`CLAUDE.md` — **both** the "Field injection mechanism" and "Constructor injection
mechanism" sections are rewritten from scratch (the second describes
`ctor_autowired_params` computed at registration time, equally dead), the module table
gains `plans.py` and loses the `Scope` line, and new cycle-policy and thread-safety
sections carry the entry-point-independence rule and the no-fast-path reasoning.

**No `CHANGELOG`.** With one consumer it is ceremony; this spec plus the git tag
already carry the migration story. Revisit if pywire gets a second user.

## Out of scope

Deferred to spec 2: `register_instance` / `register_factory`, `as_type=`, `@provides`.

Declared YAGNI: the diagnostic `__getattr__`, `Autowired` as a descriptor,
`PROTOTYPE`, qualifiers, per-router container overrides, a global plan cache.

## Migration notes (breaking change)

Target version `0.4.0`. `scripts/bump-version.sh minor` is **not** run as part of
implementation — per project rule it runs only on explicit request, once
implementation is complete.

The library has a single consumer (its author), so no deprecation period is provided.
Three changes a caller can trip over:

- A hand-written `Cls()` on a registered component is no longer wired: every such call
  site must go through `container.resolve(Cls)` (or `container.get(Cls)`).
- `from pywire import BeanDefinition` now fails; import it from `pywire.definitions` if
  it is genuinely needed.
- Duplicate registration raises `RegistrationError` instead of `ValueError`. It derives
  from `PyWireError`, not from `ValueError`, so an existing `except ValueError` will not
  catch it.

Anything already resolving through the container needs no change.
