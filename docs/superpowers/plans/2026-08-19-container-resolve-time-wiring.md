# Container resolve-time wiring — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `Container.register()` a pure registration operation that never mutates the registered class, and make `Container.resolve()` the single site where an instance is constructed and wired.

**Architecture:** Delete the `__new__`/`__init__` monkey-patching in `Container._instrument`. Move "what does this class need?" into a new `plans.py` (`InjectionPlan.for_class`), and put construction into `resolve()`: plan → `cls.__new__(cls)` → early-register for field cycles → inject fields → resolve constructor args → `cls.__init__(...)`. "In construction" state moves out of instance flags into a call-scoped `_Resolution` object holding a stack of `(type, edge_kind)` frames — which produces both the error chain and the cycle policy — plus a `created` undo log for rollback.

**Tech Stack:** Python 3.13, `uv`, pytest, ruff (`E, F, I, UP, RUF, ANN401`, line length 88, target `py313`), pyright (basic).

**Spec:** `docs/superpowers/specs/2026-08-18-container-resolve-time-wiring-design.md` (revised 2026-08-19)

---

## Design decisions (settled 2026-08-19, review of an earlier draft)

These are load-bearing. An implementer who "simplifies" one of them is undoing a
decision, not tidying up.

| # | Decision | Rationale |
|---|---|---|
| D1 | Call-scoped state lives in a `_Resolution` object, not on `Container` | The old design stored `_di_initializing` on user instances; storing `_resolving`/`_created` on the container repeats the category error with a different victim. `_Resolution` also makes the cycle policy and rollback testable in isolation. |
| D2 | `Container._current` (a nullable `_Resolution`) is the one exception to D1 | `resolve()`'s public signature is fixed, so a reentrant call from inside a component's `__init__` has no other way to find the stack it belongs to. One nullable field, safe because the lock admits one thread. `threading.local()` rejected as unused generality while the coarse lock exists. |
| D3 | The coarse `RLock` stays, covering user `__init__` bodies | Releasing it around `__init__` would let two threads both miss the cache and both construct the same singleton. Per-type condition variables were rejected as disproportionate. The deadlock caveat is documented, not solved. |
| D4 | `BeanDefinition.ready`, set after `__init__` returns, enables a lock-free cache hit | `instance` is published *early* (that is what makes field cycles work), so reading it without synchronisation could hand out a half-built object. `ready` is the flag a lock-free read can trust. Rollback must clear it alongside `instance`. |
| D5 | `PyWireError` is immutable; `requester` is passed **down** the recursion | The earlier draft mutated `error.requester` on the way up, so `str(exc)` changed as it propagated and an "attached exactly once" invariant had to be maintained by hand. Passing the label down means the raising frame knows both pieces of context, and `_resolve_dependency`'s try/except disappears entirely. |
| D6 | `PyWireError.with_context()` re-raises plan errors as copies | Consequence of D5: `plans.py` is pure inspection and knows nothing about the stack, so its errors arrive contextless and would otherwise lose the chain. A `dataclasses.replace`-style copy preserves immutability. |
| D7 | `UnconstructibleComponentError(PyWireError)` for structural rejections | They are not resolution failures, and they are no longer registration failures either (rejection is lazy). `RegistrationError` narrows to duplicate registration. |
| D8 | Annotation evaluation is **total**: unresolvable names become `_MissingName` placeholders | Replaces a 35-line AST classifier that parsed the annotation, recompiled its subscript base and swallowed bare `Exception` — and whose live path no test reached. One `eval` policy in one place; `resolve_autowired_type` becomes the single site that decides raise-vs-`None`. |
| D9 | `resolve_autowired_type` has a three-case contract | `Autowired[T]` resolvable → `T`; `Autowired[T]` unresolvable → raise; anything else, *including* a non-Autowired annotation containing an unresolvable name → `None`. The third case is what keeps a `TYPE_CHECKING`-only import from breaking an unrelated class. |
| D10 | `field_label()` / `param_label()` live in `plans.py` | The label format was written at four sites across two modules. `plans.py` is the module whose job *is* distinguishing fields from constructor parameters, and `container.py` already imports it. No sixth module for two f-strings. |
| D11 | Task order is 1 → 2 → 3 → **rewrite** → **planner** → 6 → 7 | In the earlier draft the planner's new rejections landed while `_instrument` still planned at *registration* time, so one commit rejected classes that work both before and after it. Swapping the two makes every commit a valid state of the library. |
| D12 | `fastapi.py` re-**resolves** deferred `Autowired[T]` parameters at request time | Raising at decoration time would reintroduce the exact failure mode the global `add_api_route` patch exists to eliminate, and would make endpoints eager while components are lazy. Re-resolving fixes both: a service defined below its route works, a genuinely missing name fails at request time naming the endpoint. |
| D13 | `Container.clear_instances()` is public | `conftest.py` and the rollback test both needed it; without it they reach into `_registry` and hand-build `BeanDefinition`s — the same `BeanDefinition` this plan removes from the public surface for being internal. |
| D14 | `_EdgeKind` is a plain `Enum`, not `StrEnum` | Nothing consumes the member values as strings. `ROOT` and `FIELD` are behaviourally identical and kept apart only so a printed stack reads unambiguously. |
| D15 | Positional-only `Autowired` parameters are rejected at plan time | Verified: they are `POSITIONAL_ONLY`, not variadic, so they pass the planner and then die on `**kwargs` with a raw `TypeError` — the exact failure class the planner exists to eliminate. |
| D16 | `__slots__` stays a *runtime* rejection while frozen is a *plan-time* one | Not an oversight: whether an attribute is settable is statically undecidable (a base class may supply `__dict__`). The docstring must say so. |
| D17 | `InjectionPlan` is `@dataclass(slots=True, eq=False)` — not `frozen` | `frozen=True` over `dict` fields is shallow (both dicts stay mutable) and generates a `__hash__` that raises `TypeError`. It advertised something it did not provide. |
| D18 | `markers.callable_hints()` is shared by `plans.py` and `fastapi.py` | Consequence of D8: both had their own `get_type_hints`-with-fallback. `fastapi.py` importing it from `markers` keeps its dependency on annotation semantics only — it never learns about `plans`. |

---

## Global Constraints

- Python `>=3.12` per `pyproject.toml`; ruff targets `py313`. Use PEP 695 syntax (`def register[T]`, `type X = ...`) as the existing code already does.
- Line length 88. Ruff rule sets `E, F, I, UP, RUF, ANN401` — in particular `ANN401` forbids bare `Any` in signatures without a `# noqa: ANN401` and a comment explaining why.
- All docstrings, comments, and exception messages in **English only**.
- **Never add a `# noqa` for a rule outside the enabled set.** `RUF100` is active and reports an unused directive as an error — verified for both `S307` and `BLE001`. `eval()` and bare `except Exception` need no suppression here.
- Relative imports inside `src/pywire/` (`from .markers import ...`); absolute in tests (`from pywire import ...`).
- Package management is `uv`. Run tests with `uv run pytest`, lint with `uv run ruff check .`, type-check with `uv run pyright`.
- **Git in this repo requires an inline safe.directory flag.** Every git command below must be prefixed:
  `git -c safe.directory=C:/Users/alessio.gilardi/PycharmProjects/Personal/pywire ...`
  The plan writes plain `git` for readability; add the flag when running.
- Commit messages follow the project's gitmoji convention (`commit-moji` skill): `<emoji> <imperative description>`, max 72 chars, **no attribution trailers of any kind**.
- **Do not run `scripts/bump-version.sh`.** The version bump to `0.4.0` happens only on the user's explicit request, after this plan is complete.
- `decorators.py` is not modified by any task.
- **Test classes whose annotations reference other test classes must be defined at module level.** Annotations are evaluated against *module* globals, so a class defined inside a test function cannot resolve a dependency also defined inside that function. This is pre-existing behavior, not something this plan changes.

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `src/pywire/exceptions.py` | Rewrite | Hierarchy rooted at `PyWireError`, immutable, with `with_context()` |
| `src/pywire/markers.py` | Rewrite | `Autowired[T]`, `_MissingName`, `evaluate_annotation`, `callable_hints`, `resolve_autowired_type` |
| `src/pywire/plans.py` | **Create** | `InjectionPlan`: what a class needs, by pure inspection; label helpers; rejects unconstructible classes |
| `src/pywire/definitions.py` | Rewrite | `BeanDefinition` gains `ready` and `plan`, loses `scope`; `Scope` deleted |
| `src/pywire/container.py` | Rewrite | Registry, `_Resolution`, construction sequence, per-subtree rollback, `clear_instances()`, lock |
| `src/pywire/fastapi.py` | Modify | Endpoint context; deferred request-time resolution of unresolvable `Autowired[T]` |
| `src/pywire/__init__.py` | Modify | Export the exception hierarchy; **stop exporting `BeanDefinition`** |
| `tests/test_exceptions.py` | **Create** | Hierarchy, immutability, `with_context`, message composition |
| `tests/test_plans.py` | **Create** | `InjectionPlan.for_class` in isolation |
| `tests/test_container_semantics.py` | **Create** | Isolation, non-invasiveness, edge cases, cycles, rollback, threads |
| `tests/conftest.py` | Modify | `autouse` fixture calling `clear_instances()` on the default container |
| `tests/test_components.py` | Modify | Real isolation test; `RegistrationError` |
| `tests/test_constructor_injection.py` | Modify | Constructor cycle now raises; collapse duplicated settings classes |
| `tests/test_markers.py` | Modify | Three-case contract for `resolve_autowired_type` |
| `tests/test_fastapi_integration.py` | Modify | Deferred resolution: late-defined service, and genuine failure at request time |
| `tests/test_circular_dependencies.py` | **Unchanged** | Field cycles stay legal; a failure here is a regression |
| `tests/test_container.py` | **Unchanged** | Verified: no `from __future__ import annotations`, and its function-local classes close over already-evaluated class objects, so field planning resolves them without module globals |
| `README.md`, `CLAUDE.md` | Modify | Documentation |

**No `CHANGELOG` is created.** With a single consumer it is ceremony; the spec plus the git tag carry the migration story.

---

### Task 1: Exception hierarchy and public surface

**Files:**
- Rewrite: `src/pywire/exceptions.py`
- Modify: `src/pywire/__init__.py`
- Test: `tests/test_exceptions.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `PyWireError(Exception)` with `message`, `chain`, `requester`, a composing `__str__`, and `with_context()`; `RegistrationError`, `UnconstructibleComponentError`, `AnnotationResolutionError`, `DependencyResolutionError` (all `PyWireError`); `CircularDependencyError(DependencyResolutionError)`. All importable from `pywire`. `BeanDefinition` is **no longer** importable from `pywire`.

Verified before writing this task: no test file imports `BeanDefinition`, so unexporting it breaks nothing in the suite.

- [ ] **Step 1: Write the failing test**

Create `tests/test_exceptions.py`:

```python
"""Tests for the pywire exception hierarchy and its message composition."""

from __future__ import annotations

import pytest

from pywire import (
    AnnotationResolutionError,
    CircularDependencyError,
    DependencyResolutionError,
    PyWireError,
    RegistrationError,
    UnconstructibleComponentError,
)


class Alpha:
    pass


class Beta:
    pass


def test_every_error_derives_from_pywire_error() -> None:
    """A caller can catch PyWireError to handle any pywire failure."""
    for error in (
        RegistrationError,
        UnconstructibleComponentError,
        AnnotationResolutionError,
        DependencyResolutionError,
        CircularDependencyError,
    ):
        assert issubclass(error, PyWireError)


def test_circular_dependency_error_is_a_resolution_error() -> None:
    """A circular dependency is a resolution failure, so catching
    DependencyResolutionError must also catch it."""
    assert issubclass(CircularDependencyError, DependencyResolutionError)


def test_unconstructible_is_not_a_resolution_error() -> None:
    """A class the container can never build is a structural defect, not a
    lookup failure, so the two must be catchable apart."""
    assert not issubclass(UnconstructibleComponentError, DependencyResolutionError)


def test_bare_error_renders_only_its_message() -> None:
    assert str(PyWireError("Something failed.")) == "Something failed."


def test_context_is_appended_not_spliced() -> None:
    """Requester and chain are composed onto the message rather than
    rewriting it, so carrying context never mangles the original text."""
    error = DependencyResolutionError(
        "Cannot resolve 'Beta'.",
        chain=(Alpha, Beta),
        requester="Alpha.beta",
    )

    rendered = str(error)

    assert rendered.startswith("Cannot resolve 'Beta'.")
    assert "Required by 'Alpha.beta'." in rendered
    assert "Resolution chain: Alpha -> Beta" in rendered


def test_with_context_returns_a_copy_of_the_same_type() -> None:
    """Planning raises contextless errors; the container re-raises them as
    copies rather than mutating them, so str() never changes under a caller."""
    original = UnconstructibleComponentError("Cannot construct 'Beta'.")

    enriched = original.with_context(chain=(Alpha, Beta), requester="Alpha.beta")

    assert enriched is not original
    assert type(enriched) is UnconstructibleComponentError
    assert original.chain == ()
    assert original.requester is None
    assert "Resolution chain: Alpha -> Beta" in str(enriched)


def test_with_context_never_overwrites_existing_context() -> None:
    """The frame that raised knew more than the frame enriching it."""
    original = DependencyResolutionError(
        "Cannot resolve 'Beta'.",
        chain=(Beta,),
        requester="Beta.self",
    )

    enriched = original.with_context(chain=(Alpha,), requester="Alpha.beta")

    assert enriched.chain == (Beta,)
    assert enriched.requester == "Beta.self"


def test_bean_definition_is_no_longer_public() -> None:
    """BeanDefinition is internal machinery; it is reachable from its own
    module, but not from the package's public surface."""
    with pytest.raises(ImportError):
        from pywire import BeanDefinition  # noqa: F401
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_exceptions.py -v`
Expected: FAIL — `ImportError: cannot import name 'PyWireError' from 'pywire'`

- [ ] **Step 3: Write the implementation**

Replace the whole of `src/pywire/exceptions.py`:

```python
from __future__ import annotations

from typing import Self, override


class PyWireError(Exception):
    """Base class for every error raised by pywire.

    Resolution context is carried as structured data and composed into the
    final text by __str__. Both pieces of context arrive at construction time:
    the frame that raises knows the chain it is standing in, and it receives
    the requester label from its caller as an argument rather than having it
    attached afterwards. The exception is therefore immutable -- its str() does
    not change as it propagates -- and there is exactly one mechanism for
    context instead of two.

    Attributes:
        message: The failure itself, without any context.
        chain: The resolution chain at the point of failure, outermost first.
        requester: "Owner.member" label of whatever asked for the failed
            dependency, or None when nothing did (a direct resolve() call).
    """

    def __init__(
        self,
        message: str,
        *,
        chain: tuple[type, ...] = (),
        requester: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.chain = chain
        self.requester = requester

    def with_context(
        self,
        *,
        chain: tuple[type, ...],
        requester: str | None,
    ) -> Self:
        """Return a copy carrying context this error was raised without.

        Planning (plans.py) is pure inspection and knows nothing about the
        resolution stack, so the errors it raises arrive contextless and would
        otherwise lose the chain entirely. The container re-raises them through
        this method instead of mutating them. Existing context always wins: the
        frame that raised knew more than the frame enriching it.
        """
        return type(self)(
            self.message,
            chain=self.chain or chain,
            requester=self.requester or requester,
        )

    @override
    def __str__(self) -> str:
        parts = [self.message]

        if self.requester is not None:
            parts.append(f"Required by '{self.requester}'.")

        if self.chain:
            names = " -> ".join(entry.__name__ for entry in self.chain)
            parts.append(f"Resolution chain: {names}")

        return " ".join(parts)


class RegistrationError(PyWireError):
    """Raised when a class cannot be registered.

    Narrowly scoped to registration itself -- currently, registering the same
    class twice in one container. Structural defects that make a class
    impossible to build are UnconstructibleComponentError instead, because they
    are detected lazily during resolve(), not at registration.
    """


class UnconstructibleComponentError(PyWireError):
    """Raised when the container cannot build a class at all.

    Its __new__ requires arguments, its __init__ has a parameter nothing can
    supply, or it forbids setting an injected field. A structural property of
    the class itself, independent of what happens to be registered alongside
    it -- which is why it is not a DependencyResolutionError.
    """


class AnnotationResolutionError(PyWireError):
    """Raised when an Autowired[...] annotation cannot be resolved to a type."""


class DependencyResolutionError(PyWireError):
    """Raised when a registered dependency is missing or fails to build."""


class CircularDependencyError(DependencyResolutionError):
    """Raised when a dependency cycle passes through a constructor parameter."""
```

Rewrite `src/pywire/__init__.py`:

```python
from .container import Container
from .decorators import (
    agent,
    client,
    component,
    get_default_container,
    repository,
    service,
)
from .exceptions import (
    AnnotationResolutionError,
    CircularDependencyError,
    DependencyResolutionError,
    PyWireError,
    RegistrationError,
    UnconstructibleComponentError,
)
from .markers import Autowired

__all__ = [
    "AnnotationResolutionError",
    "Autowired",
    "CircularDependencyError",
    "Container",
    "DependencyResolutionError",
    "PyWireError",
    "RegistrationError",
    "UnconstructibleComponentError",
    "agent",
    "client",
    "component",
    "get_default_container",
    "repository",
    "service",
]
```

`BeanDefinition` is deliberately absent: it is internal machinery with a mutable
`instance`, this redesign grows it further with `ready` and `plan`, and no test ever
imported it from the package. `Container.clear_instances()` (Task 4) is what tests
need instead.

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest && uv run ruff check . && uv run pyright`
Expected: all PASS. Nothing consumes the new names yet, so no existing behavior changes.

- [ ] **Step 5: Commit**

```bash
git add src/pywire/exceptions.py src/pywire/__init__.py tests/test_exceptions.py
git commit -m "✨ Add immutable PyWireError hierarchy with context"
```
---

### Task 2: Extract `InjectionPlan` at unchanged semantics

Extraction **plus** the switch-over, in one task. `InjectionPlan.for_class` reproduces exactly what `_instrument` computes today — own annotations only, existing fallback shape, no new rejections — and `_instrument` starts calling it. The existing green suite is then a real equivalence proof: it shows the *move* is faithful before Task 4 rewrites the thing being moved into. Do not delete this step because Task 4 follows soon after — without it, Task 4 becomes a simultaneous move-and-rewrite with nothing verifying the move.

**Files:**
- Create: `src/pywire/plans.py`
- Modify: `src/pywire/container.py:59-103,153-175`
- Test: `tests/test_plans.py` (create)

**Interfaces:**
- Consumes: `resolve_autowired_type` from `markers.py` (current signature; still returns `None` on an unresolvable forward reference — Task 3 changes that).
- Produces: `InjectionPlan` — a slotted dataclass with `fields: dict[str, type]` and `ctor_params: dict[str, type]`, plus the classmethod `InjectionPlan.for_class(target: type) -> InjectionPlan`.

There is deliberately **no** `unsatisfiable` field. A plan describes what to inject and nothing else; unconstructible classes are rejected by raising, which Task 5 introduces.

Per **D17**, the dataclass is `slots=True, eq=False` and *not* `frozen`: `frozen` over `dict` fields is shallow, and it generates a `__hash__` that raises `TypeError` on the first `hash()` call.

Note on staging: this task's `plans.py` keeps a local `_evaluate` helper with the *current* `NameError -> None` policy, so the equivalence proof holds. Task 3 deletes it in favour of `markers.evaluate_annotation` (**D8**), which deliberately changes that policy.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_plans.py`:

```python
"""Tests for InjectionPlan, the description of what a class needs.

These run without a Container: planning is pure inspection.
"""

from __future__ import annotations

from pywire import Autowired
from pywire.plans import InjectionPlan


class Dep:
    pass


class OtherDep:
    pass


class FieldOnly:
    dep: Autowired[Dep]
    untouched: int


class CtorOnly:
    def __init__(self, dep: Autowired[Dep]) -> None:
        self.dep = dep


class Both:
    field_dep: Autowired[Dep]

    def __init__(self, ctor_dep: Autowired[OtherDep]) -> None:
        self.ctor_dep = ctor_dep


class ForwardRef:
    dep: Autowired["LateDefined"]


class LateDefined:
    pass


class PlainArgWithDefault:
    def __init__(self, url: str = "sqlite://memory") -> None:
        self.url = url


class VariadicOnly:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.args = args


class MixedAnnotations:
    dep: Autowired[Dep]
    # A bare undefined name, not a string literal. Verified on 3.13.7: under
    # PEP 563 `broken: "NeverDefined"` stringifies to "'NeverDefined'", which
    # evaluates *successfully* to a plain str -- so the quoted spelling never
    # exercises the unresolvable path this test exists to cover.
    broken: NeverDefinedAnywhere  # noqa: F821


def test_plans_autowired_fields() -> None:
    plan = InjectionPlan.for_class(FieldOnly)

    assert plan.fields == {"dep": Dep}
    assert plan.ctor_params == {}


def test_plans_autowired_constructor_parameters() -> None:
    plan = InjectionPlan.for_class(CtorOnly)

    assert plan.fields == {}
    assert plan.ctor_params == {"dep": Dep}


def test_fields_and_constructor_parameters_coexist() -> None:
    plan = InjectionPlan.for_class(Both)

    assert plan.fields == {"field_dep": Dep}
    assert plan.ctor_params == {"ctor_dep": OtherDep}


def test_forward_reference_field_resolves_against_the_owning_module() -> None:
    plan = InjectionPlan.for_class(ForwardRef)

    assert plan.fields == {"dep": LateDefined}


def test_non_autowired_parameter_with_default_is_ignored() -> None:
    plan = InjectionPlan.for_class(PlainArgWithDefault)

    assert plan.ctor_params == {}


def test_variadic_parameters_are_ignored() -> None:
    plan = InjectionPlan.for_class(VariadicOnly)

    assert plan.ctor_params == {}


def test_class_without_init_or_annotations_plans_to_nothing() -> None:
    class Bare:
        pass

    plan = InjectionPlan.for_class(Bare)

    assert plan.fields == {}
    assert plan.ctor_params == {}


def test_unrelated_unresolvable_annotation_does_not_break_planning() -> None:
    """A class-level annotation that cannot be evaluated must not prevent the
    Autowired fields on the same class from being planned."""
    plan = InjectionPlan.for_class(MixedAnnotations)

    assert plan.fields == {"dep": Dep}


def test_inherited_init_is_planned_against_its_defining_module() -> None:
    """__init__ inherited from a base class resolves its annotations in the
    module where the base was defined, not where the subclass lives."""

    class Child(CtorOnly):
        pass

    plan = InjectionPlan.for_class(Child)

    assert plan.ctor_params == {"dep": Dep}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_plans.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pywire.plans'`

- [ ] **Step 3: Write `plans.py`**

Create `src/pywire/plans.py`:

```python
from __future__ import annotations

import inspect
import sys
from dataclasses import dataclass
from typing import Any, get_type_hints

from .markers import resolve_autowired_type

_VARIADIC_KINDS = (
    inspect.Parameter.VAR_POSITIONAL,
    inspect.Parameter.VAR_KEYWORD,
)


@dataclass(slots=True, eq=False)
class InjectionPlan:
    """What a class needs in order to be constructed by a Container.

    Planning is pure inspection: it reads annotations and signatures and never
    instantiates, registers, or resolves anything. A plan carries no failure
    state -- a class the container cannot construct is rejected by raising,
    not by recording a flag for someone else to check.

    Not frozen: frozen would be shallow over the two dicts, and it would
    generate a __hash__ that raises TypeError on them. eq=False keeps identity
    semantics, which is all a per-definition cache needs.

    Attributes:
        fields: Class-level Autowired fields, mapped to the type to inject.
        ctor_params: __init__ parameters annotated Autowired, mapped to the
            type to inject.
    """

    fields: dict[str, type]
    ctor_params: dict[str, type]

    @classmethod
    def for_class(cls, target: type) -> InjectionPlan:
        """Inspect target and describe everything the container must supply."""
        return cls(
            fields=_plan_fields(target),
            ctor_params=_plan_constructor(target),
        )


def _plan_fields(target: type) -> dict[str, type]:
    """Collect the class-level Autowired fields declared on target itself.

    Annotations are evaluated one at a time rather than through
    get_annotations(eval_str=True): a single unevaluable annotation (a
    TYPE_CHECKING-only import, a name defined nowhere) would otherwise raise
    NameError and discard the whole class's plan, including its perfectly
    valid Autowired fields.
    """
    module_globals = _module_globals(target)
    fields: dict[str, type] = {}

    for name, annotation in inspect.get_annotations(target).items():
        evaluated = _evaluate(annotation, module_globals)

        if evaluated is None:
            continue

        field_type = resolve_autowired_type(evaluated, module_globals)

        if field_type is not None:
            fields[name] = field_type

    return fields


def _plan_constructor(target: type) -> dict[str, type]:
    """Collect the Autowired parameters of target's __init__."""
    original_init = target.__init__

    if original_init is object.__init__:
        return {}

    hints = _init_hints(original_init)
    init_globals = getattr(original_init, "__globals__", {})
    parameters = list(inspect.signature(original_init).parameters.values())
    ctor_params: dict[str, type] = {}

    # Skip "self", the first parameter of an instance __init__.
    for parameter in parameters[1:]:
        if parameter.kind in _VARIADIC_KINDS:
            continue

        param_type = resolve_autowired_type(hints.get(parameter.name), init_globals)

        if param_type is not None:
            ctor_params[parameter.name] = param_type

    return ctor_params


def _init_hints(original_init: Any) -> dict[str, Any]:  # noqa: ANN401
    """Resolve __init__'s annotations, tolerating unresolvable ones.

    No explicit globalns is passed: original_init may be inherited from a base
    class defined in a different module, so the subclass's module would be the
    wrong resolution context. get_type_hints() reads original_init.__globals__
    internally, which is always the module __init__ was defined in.

    get_type_hints() evaluates every annotation at once, so one unresolvable
    annotation would discard them all. The fallback resolves them one at a
    time; a parameter that still cannot be resolved is simply absent, and is
    then treated like any other non-injected parameter.

    Any is unavoidable here: original_init is an arbitrary user callable and
    typeshed's get_type_hints() is itself Any-returning.
    """
    try:
        return get_type_hints(original_init, include_extras=True)
    except NameError:
        pass

    hints: dict[str, Any] = {}
    init_globals = getattr(original_init, "__globals__", {})

    for name, annotation in getattr(original_init, "__annotations__", {}).items():
        evaluated = _evaluate(annotation, init_globals)

        if evaluated is not None:
            hints[name] = evaluated

    return hints


def _module_globals(owner: type) -> dict[str, Any]:
    """Return the globals of the module owner was defined in.

    Falls back to an empty mapping for classes whose module is not importable
    (built dynamically, or defined in an exec'd string).
    """
    module = sys.modules.get(owner.__module__)

    return getattr(module, "__dict__", {})


def _evaluate(
    annotation: object,
    module_globals: dict[str, Any],
) -> Any | None:  # noqa: ANN401
    """Evaluate a possibly-stringified annotation, or None if it cannot be.

    Deliberately reproduces the current NameError -> None policy so this task
    is a faithful extraction. Task 3 replaces it with markers.evaluate_
    annotation, which is total and changes that policy on purpose.

    Returns Any because it passes through whatever eval() produced, exactly as
    the annotation machinery it feeds does.
    """
    if not isinstance(annotation, str):
        return annotation

    try:
        return eval(annotation, module_globals)
    except NameError:
        return None
```

- [ ] **Step 4: Switch `_instrument` onto the plan**

In `src/pywire/container.py`, add `from .plans import InjectionPlan`, then replace the head of `_instrument` (lines 62-103, everything from `raw_annotations = ...` through the `ctor_autowired_params = {...}` block) with:

```python
    def _instrument(self, cls: type) -> None:
        """Install the __new__ and __init__ needed for field injection."""

        plan = InjectionPlan.for_class(cls)
        original_init = cls.__init__
        original_new: Any = cls.__new__
```

Replace the field loop inside the nested `init`:

```python
            for field_name, field_type in plan.fields.items():
                setattr(instance, field_name, self.resolve(field_type))
```

and the constructor-kwargs comprehension:

```python
            # Explicitly passed keyword arguments win over auto-resolution.
            resolved_kwargs = {
                name: self.resolve(target)
                for name, target in plan.ctor_params.items()
                if name not in kwargs
            }
```

Remove the now-unused `inspect`, `sys`, `get_type_hints` and `resolve_autowired_type` imports; ruff's `F401` will flag any left behind.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -v`
Expected: all tests PASS, unchanged. If any fails, `InjectionPlan` differs from the logic it replaced — fix `plans.py`, not the test. This step is the equivalence proof; do not proceed past a red suite.

- [ ] **Step 6: Lint and type-check**

Run: `uv run ruff check . && uv run pyright`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/pywire/plans.py src/pywire/container.py tests/test_plans.py
git commit -m "♻️ Extract InjectionPlan and source injection from it"
```

---

### Task 3: One annotation-evaluation policy

Three code paths evaluated annotations with three different failure policies. This task collapses them into one total evaluator in `markers.py` (**D8**), gives `resolve_autowired_type` a single three-case contract (**D9**), and makes broken `Autowired` references fail loudly instead of silently skipping injection — except on FastAPI endpoints, where the failure is deferred to request time so decoration never breaks (**D12**).

**Files:**
- Rewrite: `src/pywire/markers.py`
- Modify: `src/pywire/plans.py` (delete `_evaluate` and `_init_hints`, add label helpers, pass context)
- Modify: `src/pywire/fastapi.py`
- Test: `tests/test_markers.py`, `tests/test_fastapi_integration.py`

**Interfaces:**
- Consumes: `AnnotationResolutionError` from Task 1.
- Produces:
  - `evaluate_annotation(annotation, module_globals) -> Any` — total; never raises. Unresolvable names become `_MissingName` placeholders.
  - `callable_hints(func) -> dict[str, Any]` — `get_type_hints` with a per-annotation fallback through `evaluate_annotation`. Shared by `plans.py` and `fastapi.py` (**D18**).
  - `resolve_autowired_type(annotation, module_globals, context: str | None = None)` — three cases, per **D9**.
  - `plans.field_label(owner, name)` and `plans.param_label(owner, name)` (**D10**).

- [ ] **Step 1: Write the failing tests**

Replace the imports of `tests/test_markers.py` with `from typing import get_args, get_origin, get_type_hints`, `import pytest`, `from pywire import AnnotationResolutionError, Autowired`, and `from pywire.markers import evaluate_annotation, resolve_autowired_type`. Keep the two existing tests and append:

```python
def test_autowired_with_resolvable_type_returns_it() -> None:
    """Case 1 of the contract."""
    hints = get_type_hints(_Resolvable, include_extras=True)

    assert resolve_autowired_type(hints["dependency"], globals()) is Target


def test_unresolvable_autowired_reference_raises() -> None:
    """Case 2. Autowired["Missing"] is a broken annotation, not an absent one:
    it must fail loudly instead of silently skipping injection."""
    annotation = evaluate_annotation('Autowired["Missing"]', globals())

    with pytest.raises(AnnotationResolutionError) as excinfo:
        resolve_autowired_type(annotation, {"__name__": "tests.fake"})

    message = str(excinfo.value)
    assert "Missing" in message
    assert "tests.fake" in message


def test_unresolvable_bare_autowired_reference_raises_the_same_way() -> None:
    """Case 2 again. The quoted and unquoted spellings of the same mistake
    must produce one error from one code path, not two."""
    annotation = evaluate_annotation("Autowired[Missing]", globals())

    with pytest.raises(AnnotationResolutionError):
        resolve_autowired_type(annotation, {"__name__": "tests.fake"})


def test_non_autowired_annotation_returns_none() -> None:
    """Case 3. A plain annotation is not an error; it is simply not injected."""
    assert resolve_autowired_type(int, {}) is None


def test_unresolvable_name_outside_autowired_returns_none() -> None:
    """Case 3 again, and the reason evaluation is total: an unresolvable name
    in an annotation pywire does not own -- a TYPE_CHECKING-only import, say --
    is nobody's error."""
    annotation = evaluate_annotation("list[Missing]", {"list": list})

    assert get_origin(annotation) is list
    assert resolve_autowired_type(annotation, {}) is None


def test_evaluation_is_total() -> None:
    """Every annotation yields a value, so one broken annotation can never
    discard a whole class's plan."""
    for source in ("Missing", "pkg.Thing", "not valid python (", "int | Missing"):
        assert evaluate_annotation(source, {"int": int}) is not None


def test_evaluation_passes_non_strings_through_untouched() -> None:
    assert evaluate_annotation(int, {}) is int
```

Add the helper class this needs, at module level:

```python
class _Resolvable:
    dependency: Autowired[Target]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_markers.py -v`
Expected: FAIL — `evaluate_annotation` does not exist, and `resolve_autowired_type` currently returns `None` instead of raising.

- [ ] **Step 3: Rewrite `markers.py`**

Replace the whole of `src/pywire/markers.py`:

```python
from __future__ import annotations

import builtins
from typing import Annotated, Any, get_args, get_origin, get_type_hints, override

from .exceptions import AnnotationResolutionError


class _AutowiredMeta:
    """Sentinel tag carried as Annotated metadata to mark injected fields."""

    __slots__ = ()

    @override
    def __repr__(self) -> str:
        return "Autowired"


_AUTOWIRED = _AutowiredMeta()

# PEP 695 type alias: static type checkers see the wrapped type T directly
# instead of an opaque marker, while the container recovers the _AUTOWIRED
# tag at runtime via typing.get_origin (origin is this alias itself, not
# Annotated) followed by typing.get_args to extract T.
#
# Example:
#     class Repository:
#         client: Autowired[DBClient]
type Autowired[T] = Annotated[T, _AUTOWIRED]


class _MissingName:
    """Placeholder standing in for a name an annotation could not resolve.

    Substituting a placeholder instead of letting eval() raise makes annotation
    evaluation total: every annotation yields a value, and whether the failure
    matters is decided afterwards, in one place, by resolve_autowired_type.
    Attribute access chains, so a dotted reference such as pkg.Thing produces a
    single placeholder that still knows its full name.
    """

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __getattr__(self, attribute: str) -> _MissingName:
        return _MissingName(f"{self.name}.{attribute}")

    @override
    def __repr__(self) -> str:
        return f"<unresolved {self.name}>"


class _MissingNames(dict[str, Any]):
    """eval() locals mapping that manufactures placeholders for unknown names.

    Passed as *locals*, which is consulted before globals -- so it has to
    delegate to the real module globals and then to builtins before inventing
    anything. Without that delegation it would shadow every name in the
    annotation, Autowired included.

    A dict subclass rather than a plain Mapping on purpose: CPython reads an
    exact dict through PyDict_GetItem, which never consults __missing__, but
    reads a subclass through PyObject_GetItem, which does.
    """

    __slots__ = ("_globals",)

    def __init__(self, module_globals: dict[str, Any]) -> None:
        super().__init__()
        self._globals = module_globals

    def __missing__(self, key: str) -> Any:  # noqa: ANN401
        """Resolve key against real globals, then builtins, else placeholder.

        Any is the honest return type: this hands back whatever the module
        happens to have bound to that name.
        """
        if key in self._globals:
            return self._globals[key]

        if hasattr(builtins, key):
            return getattr(builtins, key)

        return _MissingName(key)


def evaluate_annotation(
    annotation: object,
    module_globals: dict[str, Any],
) -> Any:  # noqa: ANN401
    """Evaluate a possibly-stringified annotation. Never raises.

    Unresolvable names become _MissingName placeholders rather than NameError,
    so a single unevaluable annotation cannot discard a whole class's plan and
    every caller receives something to classify. Any other failure -- a syntax
    error, or an operator that rejects a placeholder such as `int | Missing` --
    yields one placeholder standing for the entire expression, which then
    classifies as "not Autowired" and is skipped like any unrelated annotation.

    Returns Any because it passes through whatever eval() produced, exactly as
    the annotation machinery it feeds does.
    """
    if not isinstance(annotation, str):
        return annotation

    try:
        return eval(annotation, module_globals, _MissingNames(module_globals))
    except Exception:
        return _MissingName(annotation)


def callable_hints(func: object) -> dict[str, Any]:
    """Resolve a callable's annotations, tolerating unresolvable ones.

    get_type_hints() is tried first because it handles cases the plain
    evaluator does not, such as implicit Optional. No explicit globalns is
    passed: func may be an __init__ inherited from a base class in a different
    module, so the owning class's module would be the wrong resolution context.
    get_type_hints() reads func.__globals__ internally, which is always the
    module func was defined in.

    get_type_hints() evaluates every annotation at once, so one unresolvable
    annotation would discard them all. The fallback evaluates them one at a
    time through evaluate_annotation, which is total -- so unlike the old
    fallback it never silently drops a parameter.
    """
    try:
        return get_type_hints(func, include_extras=True)
    except NameError:
        pass

    func_globals = getattr(func, "__globals__", {})

    return {
        name: evaluate_annotation(annotation, func_globals)
        for name, annotation in getattr(func, "__annotations__", {}).items()
    }


def resolve_autowired_type(
    annotation: object,
    module_globals: dict[str, Any],
    context: str | None = None,
) -> Any | None:  # noqa: ANN401
    """Return the wrapped type if annotation is Autowired[T], else None.

    Three outcomes, and only one of them is an error:

    1. Autowired[T] with T resolvable -> T.
    2. Autowired[T] with T unresolvable -> AnnotationResolutionError. Returning
       None here would be indistinguishable from "not Autowired", which would
       silently skip the injection the annotation asked for.
    3. Anything else -> None. That includes a non-Autowired annotation which
       itself contains an unresolvable name (list[Missing], a
       TYPE_CHECKING-only import): not pywire's business.

    Args:
        annotation: The annotation to inspect, already evaluated.
        module_globals: Globals a forward reference is evaluated against.
        context: Optional "Owner.member" label, interpolated into the error
            message so a broken annotation names the code that carries it.

    Returns:
        T for case 1, None for case 3.

    Raises:
        AnnotationResolutionError: case 2.

    The return type stays Any: it passes through whatever get_args() extracted,
    unexamined, and typeshed's own get_args() is Any-returning.
    """
    if get_origin(annotation) is not Autowired:
        return None

    (wrapped,) = get_args(annotation)

    # A forward reference *inside* Autowired["X"] is a string literal, not a
    # name, so annotation evaluation never touched it -- get_type_hints and
    # eval_str only evaluate the outer annotation string. Under the PEP 695
    # alias such a reference surfaces as a plain str, not a ForwardRef.
    if isinstance(wrapped, str):
        wrapped = evaluate_annotation(wrapped, module_globals)

    if isinstance(wrapped, _MissingName):
        where = f" on {context!r}" if context else ""
        module_name = module_globals.get("__name__", "<unknown>")

        raise AnnotationResolutionError(
            f"Cannot resolve Autowired[{wrapped.name}]{where}: name "
            f"{wrapped.name!r} is not defined in module {module_name!r}."
        )

    return wrapped
```

- [ ] **Step 4: Simplify `plans.py` onto the shared evaluator**

In `src/pywire/plans.py`: delete `_evaluate` **and** `_init_hints`, drop the
`get_type_hints` import, and change the `markers` import to:

```python
from .markers import callable_hints, evaluate_annotation, resolve_autowired_type
```

Add the two label helpers (**D10**) — the single definition of a format previously
written at four sites:

```python
def field_label(owner: type, name: str) -> str:
    """Return the "Owner.field" label used in error messages."""
    return f"{owner.__qualname__}.{name}"


def param_label(owner: type, name: str) -> str:
    """Return the "Owner.__init__(param)" label used in error messages."""
    return f"{owner.__qualname__}.__init__({name})"
```

Rewrite the two loop bodies. `_plan_fields` loses its `if evaluated is None` branch
entirely — evaluation is now total, so there is no "could not evaluate" case left to
handle here:

```python
    for name, annotation in inspect.get_annotations(target).items():
        evaluated = evaluate_annotation(annotation, module_globals)
        field_type = resolve_autowired_type(
            evaluated, module_globals, field_label(target, name)
        )

        if field_type is not None:
            fields[name] = field_type
```

and in `_plan_constructor`, replace the `_init_hints(original_init)` call with
`callable_hints(original_init)` and the resolution with:

```python
        param_type = resolve_autowired_type(
            hints.get(parameter.name),
            init_globals,
            param_label(target, parameter.name),
        )
```

- [ ] **Step 5: Defer the FastAPI failure to request time**

In `src/pywire/fastapi.py`, change the `markers` import to
`from .markers import callable_hints, resolve_autowired_type`, add
`from .exceptions import AnnotationResolutionError`, and replace `get_type_hints(func,
include_extras=True)` at line 39 with `callable_hints(func)` — the same shared
evaluator, so an unrelated unresolvable annotation on any endpoint in the process can
no longer abort route registration.

Add the deferred resolver next to `_resolve_autowired`:

```python
def _resolve_autowired_late(
    annotation: object,
    module_globals: dict[str, Any],
    context: str,
) -> Callable[[Request], Any]:
    """Defer a currently-unresolvable Autowired[T] parameter to request time.

    Mirrors the container's lazy planning: an endpoint may inject a service
    defined further down its own module, and by the time a request arrives that
    module is fully imported, so re-running the resolution succeeds. Raising at
    decoration time instead would reintroduce the decoration-time failure the
    global add_api_route patch exists to eliminate, and would leave endpoints
    eager while components are lazy.

    A genuinely undefined name still fails -- on the first request, naming the
    endpoint. The resolved target is memoised, so the retry happens once rather
    than per request.
    """
    resolved: list[type] = []

    def resolve(request: Request) -> Any:  # noqa: ANN401
        if not resolved:
            resolved.append(
                cast(type, resolve_autowired_type(annotation, module_globals, context))
            )

        container = getattr(request.app.state, "pywire_container", None)

        return (container or get_default_container()).resolve(resolved[0])

    return resolve
```

Replace the parameter loop in `_wire_endpoint` with:

```python
    for name, param in sig.parameters.items():
        annotation = hints.get(name)
        context = f"{func.__qualname__}({name})"

        try:
            target = resolve_autowired_type(annotation, module_globals, context)
        except AnnotationResolutionError:
            # The name may simply be defined further down this module; retry on
            # the first request rather than failing this decoration.
            changed = True
            new_params.append(
                param.replace(
                    # Inert: the Depends default means FastAPI never parses this
                    # parameter from the request, so the annotation only has to
                    # be something it accepts. The real type is unknown here by
                    # definition.
                    annotation=object,
                    default=Depends(
                        _resolve_autowired_late(annotation, module_globals, context)
                    ),
                )
            )
            continue

        if target is None:
            new_params.append(param)
            continue

        changed = True
        new_params.append(
            param.replace(
                annotation=target, default=Depends(_resolve_autowired(target))
            )
        )
```

- [ ] **Step 6: Add the FastAPI tests**

Append to `tests/test_fastapi_integration.py` (add `AnnotationResolutionError` to its
`pywire` import). Note both assertions are on the **request**, not the decoration —
that is the invariant being protected:

```python
# Decorated at *import* time, above the service it injects -- which is the only
# shape that actually reaches the deferred path. A route decorated inside a test
# body runs after the module has finished importing, so its annotation resolves
# immediately and the deferred path is never exercised. app.get() goes straight
# to app.router.add_api_route, so the endpoint is wired exactly once; do not
# rewrite this as an APIRouter + include_router, which wires it twice.
_late_app = FastAPI()


@_late_app.get("/late")
def _late_endpoint(service: Autowired["LateDefinedService"]) -> dict[str, str]:
    return {"value": service.value()}


class LateDefinedService:
    """Defined below the route that injects it, on purpose."""

    def value(self) -> str:
        return "late"


def test_endpoint_can_inject_a_service_defined_below_it() -> None:
    """Decoration must not require the injected type to exist yet -- the
    container's own planning is lazy for exactly this reason."""
    container = Container()
    container.register(LateDefinedService)

    wire(_late_app, container=container)

    response = TestClient(_late_app).get("/late")

    assert response.status_code == 200
    assert response.json() == {"value": "late"}


def test_unresolvable_autowired_parameter_fails_at_request_time() -> None:
    """A genuinely broken annotation must not break decoration; it fails on the
    first request, with a pywire error naming the endpoint."""
    app = FastAPI()

    @app.get("/broken")
    def broken(service: Autowired["NoSuchService"]) -> dict[str, str]:  # noqa: F821
        return {"ok": "yes"}

    with pytest.raises(AnnotationResolutionError) as excinfo:
        TestClient(app).get("/broken")

    assert "broken" in str(excinfo.value)
```

`LateDefinedService` must be module-level, because the annotation string is evaluated
against module globals — and it must stay **below** `_late_endpoint`, because the gap
between decoration (during import, name absent) and resolution (first request, name
present) *is* the behaviour under test. Moving it above the route, or moving the
decoration into the test body, silently turns this into a duplicate of the ordinary
path. The second test needs no such care: `NoSuchService` never exists, so it takes the
deferred path wherever it is decorated.

- [ ] **Step 7: Run everything**

Run: `uv run pytest && uv run ruff check . && uv run pyright`
Expected: all PASS. If an existing test now raises `AnnotationResolutionError`, it was relying on a silently skipped injection — report it rather than papering over it.

- [ ] **Step 8: Commit**

```bash
git add src/pywire/markers.py src/pywire/plans.py src/pywire/fastapi.py \
        tests/test_markers.py tests/test_fastapi_integration.py
git commit -m "🐛 Unify annotation evaluation and raise on broken refs"
```
---

### Task 4: ⚠️ The rewrite — resolve-time construction

**This is the point of no return.** The old and new mechanisms cannot coexist on the same class, so `container.py` and `definitions.py` change together and the suite goes red in the middle of this task on purpose.

Ordered **before** the real planner (**D11**). At this point `InjectionPlan` still rejects nothing, so a class such as `NeedsArg` keeps failing exactly as it does on `main` — with a raw `TypeError`. That is deliberate: every commit stays a valid state of the library, and Task 5 then adds the rejections into an already-lazy planner.

**Files:**
- Rewrite: `src/pywire/container.py`, `src/pywire/definitions.py`
- Modify: `tests/test_components.py` (duplicate registration), `tests/test_constructor_injection.py:93-118,148-163`

**Interfaces:**
- Consumes: `InjectionPlan`, `field_label`, `param_label` (Tasks 2/3), the exception hierarchy (Task 1).
- Produces: unchanged public signatures — `Container.register[T](cls: type[T]) -> type[T]`, `Container.resolve[T](target_type: type[T]) -> T`, `Container.get[T](target_type: type[T]) -> T` — plus the new `Container.clear_instances() -> None` (**D13**). `BeanDefinition(cls, instance=None, ready=False, plan=None)`. `Scope` no longer exists.

- [ ] **Step 1: Update the two tests whose semantics change**

In `tests/test_components.py`, `test_duplicate_registration_raises_error`: replace `pytest.raises(ValueError, match=...)` with `pytest.raises(RegistrationError, match="is already registered")` and import `RegistrationError` from `pywire`. Note `RegistrationError` is **not** a `ValueError` — that is the point of Task 1's hierarchy.

In `tests/test_constructor_injection.py`, replace the circular test (currently `:148-163`) with:

```python
def test_circular_constructor_dependencies_raise_with_the_chain():
    """A cycle through constructor parameters has no fixed point: the
    argument must be resolved before __init__ can run. It fails naming the
    chain instead of injecting a half-constructed partner."""
    container = Container()

    container.register(CircularA)
    container.register(CircularB)

    with pytest.raises(CircularDependencyError) as excinfo:
        container.resolve(CircularA)

    assert "CircularA -> CircularB -> CircularA" in str(excinfo.value)
```

Import `CircularDependencyError` from `pywire` in that file.

- [ ] **Step 2: Collapse the duplicated settings classes**

Still in `tests/test_constructor_injection.py`, delete the seven-line comment at `:93-99` and replace the four classes at `:100-118` with one pair:

```python
class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=None)

    db_url: str = "sqlite://memory"


class SettingsConsumer:
    def __init__(self, settings: Autowired[AppSettings]) -> None:
        self.settings = settings
```

Update both tests that used `AppSettingsWithEnv`/`AppSettingsDefault` and `SettingsConsumerWithEnv`/`SettingsConsumerDefault`. The duplication existed only to dodge the cross-container leak this task removes — the deleted comment says so explicitly, which is why it goes with them.

- [ ] **Step 3: Rewrite `definitions.py`**

Replace the whole file:

```python
from __future__ import annotations

from dataclasses import dataclass

from .plans import InjectionPlan


@dataclass(slots=True)
class BeanDefinition:
    """Metadata and runtime state of a registered component.

    Attributes:
        cls: The registered class.
        instance: The singleton, once created. Published *before* __init__ runs,
            so a dependency cycle closing through a field can find it.
        ready: True once __init__ has returned. Because `instance` is published
            early, it alone is not enough to hand the object out without
            synchronisation -- a reader could observe a half-built object.
            `ready` is what an unsynchronised read can trust. Rollback clears
            it together with `instance`.
        plan: Cached InjectionPlan, computed on first resolution rather than at
            registration time: a forward reference to a class defined later in
            the module can only be resolved late. Never cleared, by rollback or
            by clear_instances(), a plan being a pure function of the class.
    """

    cls: type
    instance: object | None = None
    ready: bool = False
    plan: InjectionPlan | None = None
```

`Scope` is deleted. It was never exported from `__init__.py`, so no export changes.

- [ ] **Step 4: Rewrite `container.py`**

Replace the whole file:

```python
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import cast

from .definitions import BeanDefinition
from .exceptions import (
    CircularDependencyError,
    DependencyResolutionError,
    PyWireError,
    RegistrationError,
    UnconstructibleComponentError,
)
from .plans import InjectionPlan, field_label, param_label

type Registry = dict[type, BeanDefinition]


class _EdgeKind(Enum):
    """How a resolution frame was entered.

    Only the distinction between CTOR and the rest carries behavior: a cycle is
    illegal exactly when one of its edges is a constructor argument. ROOT and
    FIELD are behaviourally identical, kept apart only so a printed stack reads
    unambiguously while debugging. A plain Enum, not a StrEnum: nothing consumes
    these as strings.
    """

    ROOT = auto()
    FIELD = auto()
    CTOR = auto()


type _Frame = tuple[type, _EdgeKind]


@dataclass(slots=True)
class _Resolution:
    """State of one in-flight resolve() call.

    This is call-scoped data, not container data: the stack of frames being
    built and the undo log of what has been created so far belong to a single
    top-level resolution and are discarded when it ends. Keeping them here
    rather than on Container is what makes the cycle policy and the rollback
    testable without a container, and it is the same mistake -- construction
    bookkeeping stored on a long-lived object -- that the deleted
    _di_initializing instance flags used to make.
    """

    stack: list[_Frame] = field(default_factory=list)
    created: list[BeanDefinition] = field(default_factory=list)

    def position_of(self, target_type: type) -> int | None:
        """Return target_type's index on the stack, if it is being built."""
        for index, (entry, _) in enumerate(self.stack):
            if entry is target_type:
                return index

        return None

    def chain(self) -> tuple[type, ...]:
        """Return the current resolution chain, outermost first."""
        return tuple(entry for entry, _ in self.stack)

    def chain_through(self, target_type: type, start: int = 0) -> tuple[type, ...]:
        """Return the chain from start, ending at target_type."""
        return (*(entry for entry, _ in self.stack[start:]), target_type)


class Container:
    """Dependency Injection container.

    register() records; resolve() constructs. A registered class is never
    modified, so each Container owns a genuinely independent singleton scope
    and a hand-written Cls() stays plain Python -- unwired.
    """

    def __init__(self) -> None:
        self._registry: Registry = dict()
        # Reentrant: resolution recurses into itself for every dependency. Held
        # across the whole construction path, including user __init__ bodies,
        # because that is what guarantees one singleton per type -- releasing it
        # around __init__ would let two threads both miss the cache and both
        # build. The documented cost is that a component whose __init__ waits on
        # another thread's resolve() deadlocks.
        self._lock = threading.RLock()
        # The resolution in flight, or None. Container state only because the
        # public resolve() signature is fixed, so a reentrant call made from
        # inside a component's __init__ has no other way to find the stack it
        # belongs to. Safe as a single field because the lock admits one thread.
        self._current: _Resolution | None = None

    def register[T](self, cls: type[T]) -> type[T]:
        """Register a class as a component.

        The class is neither instantiated nor modified. Both happen lazily,
        inside resolve().
        """
        with self._lock:
            if cls in self._registry:
                raise RegistrationError(
                    f"Component '{cls.__name__}' is already registered "
                    "in this container."
                )

            self._registry[cls] = BeanDefinition(cls=cls)

        return cls

    def resolve[T](self, target_type: type[T]) -> T:
        """Return the singleton associated with target_type, building it (and
        everything it needs) on first call."""
        definition = self._registry.get(target_type)

        if definition is not None and definition.ready:
            # Unsynchronised fast path, safe only because `ready` is set after
            # __init__ returned: reading `instance` alone would risk handing out
            # the early-published, half-built object. This relies on the GIL
            # ordering the two writes; a free-threaded build would need an
            # explicit acquire/release pair here.
            return cast(T, definition.instance)

        with self._lock:
            outermost = self._current is None

            if outermost:
                self._current = _Resolution()

            resolution = self._current
            # A resolve() reached from inside a component's __init__ is, for
            # cycle purposes, a constructor edge: whatever it returns is used
            # immediately, so it has to be complete.
            edge = _EdgeKind.CTOR if resolution.stack else _EdgeKind.ROOT

            try:
                return cast(T, self._resolve(target_type, edge, None, resolution))
            finally:
                if outermost:
                    self._current = None

    def get[T](self, target_type: type[T]) -> T:
        """Readable alias for resolve()."""
        return self.resolve(target_type)

    def clear_instances(self) -> None:
        """Drop every cached singleton, keeping every registration.

        Cached InjectionPlans survive: a plan is a pure function of the class
        and cannot go stale. Intended for test isolation of the module-level
        default container -- which @component writes into and nothing else ever
        resets -- and for any caller that wants a fresh object graph without
        rebuilding the registry.
        """
        with self._lock:
            for definition in self._registry.values():
                definition.instance = None
                definition.ready = False

    def _resolve(
        self,
        target_type: type,
        edge: _EdgeKind,
        requester: str | None,
        resolution: _Resolution,
    ) -> object:
        """Return target_type's singleton, constructing it if necessary.

        `requester` travels *down* the recursion rather than being attached to
        an exception on the way back up, so the frame that raises already knows
        both pieces of context and the exception can stay immutable.
        """
        definition = self._registry.get(target_type)

        if definition is None:
            name = getattr(target_type, "__name__", target_type)

            raise DependencyResolutionError(
                f"Cannot resolve '{name}': it is not registered "
                "in this container.",
                chain=resolution.chain_through(target_type),
                requester=requester,
            )

        position = resolution.position_of(target_type)

        if position is not None:
            self._reject_constructor_cycle(
                target_type, position, edge, requester, resolution
            )

            if definition.instance is None:
                # Reachable only through a user __new__ that resolves during
                # construction: nothing else runs between pushing the frame and
                # publishing the instance. Kept rather than deleted because the
                # alternative is silently injecting None.
                raise CircularDependencyError(
                    f"Circular dependency on '{target_type.__name__}' closed "
                    "before its instance existed: __new__ must not resolve "
                    "dependencies.",
                    chain=resolution.chain_through(target_type, start=position),
                    requester=requester,
                )

            # A legal field cycle: the partner is still under construction, but
            # its identity is final, which is all a stored reference needs.
            return definition.instance

        if definition.instance is not None:
            return definition.instance

        return self._create(target_type, definition, edge, requester, resolution)

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
            if definition.plan is None:
                # Planned before allocating: a class that cannot be planned is
                # never created and never published.
                definition.plan = self._plan(target_type, requester, resolution)

            instance = target_type.__new__(target_type)

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

            # Last, and only on the success path: this is what the
            # unsynchronised fast path in resolve() reads.
            definition.ready = True

            return instance
        except BaseException:
            self._roll_back(resolution, created_mark)
            raise
        finally:
            resolution.stack.pop()

    def _plan(
        self,
        target_type: type,
        requester: str | None,
        resolution: _Resolution,
    ) -> InjectionPlan:
        """Plan target_type, adding this resolution's context to any failure.

        Planning is pure inspection and knows nothing about the stack, so the
        errors it raises arrive contextless. They are re-raised as copies
        carrying the chain rather than mutated in place -- see
        PyWireError.with_context.
        """
        try:
            return InjectionPlan.for_class(target_type)
        except PyWireError as error:
            raise error.with_context(
                chain=resolution.chain(), requester=requester
            ) from error

    def _roll_back(self, resolution: _Resolution, created_mark: int) -> None:
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
        forever, which is worse than the problem `ready` solves. `plan` is
        deliberately preserved: it is a pure function of the class, and a
        construction failure says nothing about its validity.
        """
        for definition in resolution.created[created_mark:]:
            definition.instance = None
            definition.ready = False

        del resolution.created[created_mark:]

    def _inject_fields(
        self,
        instance: object,
        plan: InjectionPlan,
        target_type: type,
        resolution: _Resolution,
    ) -> None:
        """Set every planned Autowired field on the fresh instance."""
        for name, field_type in plan.fields.items():
            label = field_label(target_type, name)
            value = self._resolve(field_type, _EdgeKind.FIELD, label, resolution)

            try:
                setattr(instance, name, value)
            except AttributeError as exc:
                # Plan time rejects frozen dataclasses, which are detectable
                # from the class. This catches whatever else forbids assignment
                # -- __slots__ without a slot for the field being the common one
                # -- because that is not statically decidable: a base class may
                # supply __dict__.
                raise UnconstructibleComponentError(
                    f"Cannot inject field '{name}' into "
                    f"'{target_type.__name__}': the attribute cannot be set. "
                    "Use constructor injection instead.",
                    chain=resolution.chain(),
                ) from exc

    def _resolve_ctor_args(
        self,
        plan: InjectionPlan,
        target_type: type,
        resolution: _Resolution,
    ) -> dict[str, object]:
        """Resolve the Autowired constructor parameters."""
        return {
            name: self._resolve(
                dep_type,
                _EdgeKind.CTOR,
                param_label(target_type, name),
                resolution,
            )
            for name, dep_type in plan.ctor_params.items()
        }

    def _reject_constructor_cycle(
        self,
        target_type: type,
        position: int,
        edge: _EdgeKind,
        requester: str | None,
        resolution: _Resolution,
    ) -> None:
        """Refuse a cycle that passes through a constructor parameter.

        The check is on the *cycle*, not on the current frame. Checking the
        frame ("is this dependency on the stack?") would make a mixed
        field/constructor cycle succeed or fail depending on which type was
        resolved first, since only one of the two entry points reaches the cycle
        through the constructor edge.

        `edge` is included in the scan because the edge closing the cycle has
        not been pushed onto the stack yet.
        """
        kinds = [kind for _, kind in resolution.stack[position:]]
        kinds.append(edge)

        if _EdgeKind.CTOR not in kinds:
            return

        cycle = resolution.chain_through(target_type, start=position)
        rendered = " -> ".join(entry.__name__ for entry in cycle)

        raise CircularDependencyError(
            "Circular dependency through a constructor parameter: "
            f"{rendered}. Convert one of these dependencies to a field "
            "to allow the cycle.",
            chain=cycle,
            requester=requester,
        )
```

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -v`
Expected: all PASS, including the three unchanged tests in `test_circular_dependencies.py` (field cycles stay legal), the seven in `test_container.py`, and all 12 (now 14) in `test_fastapi_integration.py`. A failure in any of those files is a regression, not an intended change — fix `container.py`, not the test.

- [ ] **Step 6: Lint and type-check**

Run: `uv run ruff check . && uv run pyright`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/pywire/container.py src/pywire/definitions.py \
        tests/test_components.py tests/test_constructor_injection.py
git commit -m "💥 Move wiring from class instrumentation into resolve()"
```

---

### Task 5: The real planner — MRO, dedup, and rejections

Five behavior changes land in `plans.py`, now that planning is already lazy (**D11**), so each new rejection fires from `resolve()` where it belongs and no commit rejects a class that works either side of it. The suite stays green: verified, no existing test has an inherited `Autowired` field, a dataclass component, a positional-only `Autowired` parameter, or a non-defaulted non-`Autowired` parameter.

**Files:**
- Modify: `src/pywire/plans.py`
- Test: `tests/test_plans.py`

**Interfaces:**
- Consumes: `UnconstructibleComponentError` (Task 1), `evaluate_annotation` / `resolve_autowired_type` / label helpers (Task 3).
- Produces: `InjectionPlan.for_class` now walks the MRO, dedups constructor parameters against fields, and raises `UnconstructibleComponentError` for the four unconstructible shapes.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_plans.py`. Add `import dataclasses`, `import pytest`, and `from pywire import AnnotationResolutionError, UnconstructibleComponentError` to its imports, plus these module-level classes:

```python
class InheritedFieldBase:
    dep: Autowired[Dep]


class InheritsField(InheritedFieldBase):
    pass


class CancelsInheritedField(InheritedFieldBase):
    dep: Dep  # re-annotated without Autowired: opts out of injection


@dataclasses.dataclass
class DataclassComponent:
    dep: Autowired[Dep]


@dataclasses.dataclass(frozen=True)
class FrozenDataclassComponent:
    dep: Autowired[Dep]


@dataclasses.dataclass(frozen=True)
class FrozenFieldNotInInit:
    # Excluded from __init__, so it survives the constructor/field dedup and
    # still has to be set on a frozen instance -- the only shape that reaches
    # the frozen check.
    dep: Autowired[Dep] = dataclasses.field(init=False)


class NeedsPlainArg:
    def __init__(self, url: str) -> None:
        self.url = url


class PositionalOnlyAutowired:
    def __init__(self, dep: Autowired[Dep], /) -> None:
        self.dep = dep


class CustomNewWithArgs:
    def __new__(cls, token: str) -> CustomNewWithArgs:
        return super().__new__(cls)

    def __init__(self, token: str) -> None:
        self.token = token


class BrokenAutowiredField:
    dep: Autowired["NotAThing"]  # noqa: F821
```

and these tests:

```python
def test_field_declared_on_a_base_class_is_planned() -> None:
    """The MRO is walked base-first, so a subclass inherits its base's
    injected fields instead of silently losing them."""
    plan = InjectionPlan.for_class(InheritsField)

    assert plan.fields == {"dep": Dep}


def test_subclass_reannotation_cancels_an_inherited_injection() -> None:
    """Re-annotating without Autowired is the supported way to opt out."""
    plan = InjectionPlan.for_class(CancelsInheritedField)

    assert plan.fields == {}


def test_dataclass_field_is_planned_as_a_constructor_parameter_only() -> None:
    """A dataclass declares its fields as both class annotations and __init__
    parameters. The constructor wins, so nothing is injected twice."""
    plan = InjectionPlan.for_class(DataclassComponent)

    assert plan.fields == {}
    assert plan.ctor_params == {"dep": Dep}


def test_frozen_dataclass_with_autowired_field_is_constructible() -> None:
    """Its Autowired field is a constructor parameter, so nothing needs to be
    set on the frozen instance and the class is perfectly usable."""
    plan = InjectionPlan.for_class(FrozenDataclassComponent)

    assert plan.fields == {}
    assert plan.ctor_params == {"dep": Dep}


def test_frozen_class_with_a_non_constructor_field_is_rejected() -> None:
    """Verified shape: dataclasses.fields() reports 'dep', __init__ does not
    take it, and __dataclass_params__.frozen is True."""
    with pytest.raises(UnconstructibleComponentError) as excinfo:
        InjectionPlan.for_class(FrozenFieldNotInInit)

    assert "frozen" in str(excinfo.value)


def test_non_autowired_parameter_without_default_is_rejected() -> None:
    with pytest.raises(UnconstructibleComponentError) as excinfo:
        InjectionPlan.for_class(NeedsPlainArg)

    assert "'url'" in str(excinfo.value)


def test_positional_only_autowired_parameter_is_rejected() -> None:
    """Verified on 3.13.7: such a parameter is POSITIONAL_ONLY, not variadic,
    so it passes the planner and then dies on **kwargs with a bare TypeError --
    exactly the failure class the planner exists to eliminate."""
    with pytest.raises(UnconstructibleComponentError) as excinfo:
        InjectionPlan.for_class(PositionalOnlyAutowired)

    assert "positional-only" in str(excinfo.value)


def test_new_requiring_arguments_is_rejected() -> None:
    with pytest.raises(UnconstructibleComponentError) as excinfo:
        InjectionPlan.for_class(CustomNewWithArgs)

    assert "__new__" in str(excinfo.value)


def test_broken_autowired_field_names_the_owner() -> None:
    with pytest.raises(AnnotationResolutionError) as excinfo:
        InjectionPlan.for_class(BrokenAutowiredField)

    message = str(excinfo.value)
    assert "NotAThing" in message
    assert "BrokenAutowiredField.dep" in message
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_plans.py -v`
Expected: the nine new tests FAIL; the ones from Task 2 still PASS.

- [ ] **Step 3: Rewrite `for_class` and `_plan_fields`**

In `src/pywire/plans.py`, add `import dataclasses` and
`from .exceptions import UnconstructibleComponentError`, then replace `for_class` and
`_plan_fields`:

```python
    @classmethod
    def for_class(cls, target: type) -> InjectionPlan:
        """Inspect target and describe everything the container must supply.

        Raises:
            UnconstructibleComponentError: target cannot be constructed by the
                container at all -- its __new__ needs arguments, its __init__
                needs an argument nothing can supply, an Autowired parameter is
                positional-only, or it forbids setting an injected field.
            AnnotationResolutionError: an Autowired annotation on target names
                a type that cannot be resolved.
        """
        _reject_unconstructible_new(target)

        ctor_params = _plan_constructor(target)
        # A dataclass declares its Autowired fields as *both* class-level
        # annotations and __init__ parameters. The constructor wins: injecting
        # in both places would resolve twice and would make a frozen dataclass
        # look unconstructible when it is not.
        fields = {
            name: field_type
            for name, field_type in _plan_fields(target).items()
            if name not in ctor_params
        }

        _reject_unsettable_fields(target, fields)

        return cls(fields=fields, ctor_params=ctor_params)


def _plan_fields(target: type) -> dict[str, type]:
    """Collect the Autowired fields of target and of every class it inherits.

    The MRO is walked base-first, so a subclass annotation overrides its base's:
    re-annotating a field without Autowired is the supported way to opt out of
    an inherited injection.

    Each annotation is resolved against *its own* defining class's module
    globals. Using target's module would be wrong for an inherited annotation,
    the same trap the constructor path already avoids.

    Annotations are evaluated through markers.evaluate_annotation, which is
    total: a single unevaluable annotation (a TYPE_CHECKING-only import, a name
    defined nowhere) cannot discard the whole class's plan, and an annotation
    that fails to resolve *and* is recognisably Autowired is reported by
    resolve_autowired_type rather than skipped.
    """
    fields: dict[str, type] = {}

    for owner in reversed(target.__mro__):
        if owner is object:
            continue

        owner_globals = _module_globals(owner)

        for name, annotation in inspect.get_annotations(owner).items():
            evaluated = evaluate_annotation(annotation, owner_globals)
            field_type = resolve_autowired_type(
                evaluated, owner_globals, field_label(owner, name)
            )

            if field_type is None:
                fields.pop(name, None)
            else:
                fields[name] = field_type

    return fields


def _reject_unconstructible_new(target: type) -> None:
    """Refuse a class whose __new__ needs arguments resolve() cannot supply.

    Checked from the signature rather than by catching TypeError around
    target.__new__(target): a TypeError raised *inside* a legitimate __new__
    would otherwise be reported as "its __new__ requires arguments", which is
    actively misleading. A signature that cannot be introspected at all is
    assumed fine, and construction is left to speak for itself.
    """
    try:
        signature = inspect.signature(target.__new__)
    except (TypeError, ValueError):
        return

    named = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind not in _VARIADIC_KINDS
    ]

    # named[0], when present, is "cls". A class that does not override __new__
    # inherits object.__new__, whose signature is (*args, **kwargs) -- so
    # `named` is empty and the loop below does nothing, which is correct.
    for parameter in named[1:]:
        if parameter.default is inspect.Parameter.empty:
            raise UnconstructibleComponentError(
                f"Cannot construct '{target.__name__}': its __new__ requires "
                f"argument '{parameter.name}'. Register a pre-built instance "
                "instead."
            )


def _reject_unsettable_fields(target: type, fields: dict[str, type]) -> None:
    """Refuse a frozen class that still needs a field set on it.

    Only reachable after the constructor/field dedup: a frozen dataclass whose
    Autowired fields are all constructor parameters has nothing left to set and
    is perfectly constructible.

    Frozen is checked here, statically, because it is a property of the class.
    The other way an attribute can refuse assignment -- __slots__ with no slot
    for the field -- is *not* statically decidable, since a base class may
    supply __dict__, so it stays a runtime rejection in Container._inject_fields.
    """
    if not fields:
        return

    params = getattr(target, "__dataclass_params__", None)

    if not getattr(params, "frozen", False):
        return

    name = next(iter(fields))

    raise UnconstructibleComponentError(
        f"Cannot inject field '{name}' into frozen '{target.__name__}': "
        "use constructor injection instead."
    )
```

Replace the `if param_type is not None:` block in `_plan_constructor` with:

```python
        if param_type is not None:
            if parameter.kind is inspect.Parameter.POSITIONAL_ONLY:
                # Resolved dependencies are passed by keyword, so a
                # positional-only parameter would die on **kwargs with a bare
                # TypeError far from its cause.
                raise UnconstructibleComponentError(
                    f"Cannot construct '{target.__name__}': Autowired "
                    f"parameter '{parameter.name}' is positional-only. "
                    "Remove the '/' marker, or inject it as a field."
                )

            ctor_params[parameter.name] = param_type
        elif parameter.default is inspect.Parameter.empty:
            # resolve() passes no arguments, so a parameter that is neither
            # Autowired nor defaulted can never be satisfied. Today this
            # surfaces as a bare TypeError from Python, far from the cause.
            raise UnconstructibleComponentError(
                f"Cannot construct '{target.__name__}': parameter "
                f"'{parameter.name}' has no default and is not Autowired. "
                "Register an instance instead, or give it a default."
            )
```

`import dataclasses` is only needed if you choose to detect frozen via
`dataclasses.fields()`; the `__dataclass_params__` check above does not need it, so drop
the import if unused — `F401` will tell you.

- [ ] **Step 4: Run the plan tests**

Run: `uv run pytest tests/test_plans.py -v`
Expected: all PASS.

- [ ] **Step 5: Run everything**

Run: `uv run pytest && uv run ruff check . && uv run pyright`
Expected: all PASS. A failure elsewhere means an existing component trips one of the new rejections — report it; do not weaken the rule.

- [ ] **Step 6: Commit**

```bash
git add src/pywire/plans.py tests/test_plans.py
git commit -m "✨ Plan inherited fields and reject unconstructible classes"
```
---

### Task 6: Tests for the guarantees the rewrite creates

Additive. These encode the properties Tasks 4 and 5 made possible, which no existing test could express.

**Files:**
- Create: `tests/test_container_semantics.py`
- Modify: `tests/conftest.py`, `tests/test_components.py:78-98`

- [ ] **Step 1: Add the default-container fixture**

Replace `tests/conftest.py`:

```python
"""Pytest configuration and fixtures."""

from collections.abc import Iterator

import pytest

from pywire import get_default_container


@pytest.fixture(autouse=True)
def reset_default_container() -> Iterator[None]:
    """Isolate tests that register on the module-level default container.

    @component writes into a process-wide container that nothing ever resets, so
    without this fixture cached singletons leak between tests, invisibly and
    order-dependently.

    Registrations are deliberately kept: some test modules decorate a class with
    @component at import time, and the module is never re-imported, so dropping
    registrations would destroy those after the first test. Per-test
    registrations therefore accumulate across the session, which is harmless --
    each test body defines a distinct class object, so nothing ever collides.
    """
    yield

    get_default_container().clear_instances()
```

This is what `Container.clear_instances()` (**D13**) exists for. Do **not** revert to
snapshotting `_registry` and rebuilding `BeanDefinition`s by hand: `BeanDefinition` is
private as of Task 1, and a fixture that reaches into container internals is exactly the
implementation coupling the project's testing standard forbids.

- [ ] **Step 2: Turn the fake isolation test into a real one**

In `tests/test_components.py`, replace `test_multiple_containers_independent` (`:78-98`) with:

```python
class SharedDep:
    pass


class SharedService:
    dep: Autowired[SharedDep]


def test_multiple_containers_independent():
    """The same class registered in two containers must yield two distinct
    singletons, along with two distinct dependency graphs.

    The previous version of this test registered *different* classes in the
    two containers, so its assertion was true by construction.
    """
    container1 = Container()
    container2 = Container()

    for container in (container1, container2):
        container.register(SharedDep)
        container.register(SharedService)

    service1 = container1.resolve(SharedService)
    service2 = container2.resolve(SharedService)

    assert service1 is not service2
    assert service1.dep is not service2.dep
    assert service1.dep is container1.resolve(SharedDep)
    assert service2.dep is container2.resolve(SharedDep)
```

The two classes are module-level because `SharedService`'s annotation is resolved against module globals.

- [ ] **Step 3: Run it to verify it passes**

Run: `uv run pytest tests/test_components.py -v`
Expected: PASS. (Before Task 4 this test would have failed — that is what makes it worth writing.)

- [ ] **Step 4: Write the semantics test module**

Create `tests/test_container_semantics.py`:

```python
"""Tests for the guarantees resolve-time wiring establishes.

Each describes something the previous class-instrumentation mechanism made
impossible, so together they are the regression net for the redesign.
"""

from __future__ import annotations

import threading

import pytest

from pywire import (
    Autowired,
    CircularDependencyError,
    Container,
    DependencyResolutionError,
    UnconstructibleComponentError,
)


class Dep:
    pass


class Service:
    dep: Autowired[Dep]


class ReadsFieldInInit:
    dep: Autowired[Dep]

    def __init__(self) -> None:
        # The contract: injected fields are set before __init__ runs.
        self.seen = type(self.dep).__name__


class Slotted:
    __slots__ = ()


class NeedsArg:
    def __init__(self, url: str) -> None:
        self.url = url


class CustomNew:
    def __new__(cls, token: str) -> CustomNew:
        return super().__new__(cls)

    def __init__(self, token: str) -> None:
        self.token = token


class ResolvesInNew:
    """Pathological but legal: a __new__ that resolves during construction.

    The only way to reach the "cycle closed before its instance existed" branch
    in Container._resolve, which is why that branch is kept rather than deleted.
    """

    container: Container | None = None

    def __new__(cls) -> ResolvesInNew:
        assert cls.container is not None
        cls.container.resolve(cls)

        return super().__new__(cls)


class CtorCycleA:
    def __init__(self, b: Autowired["CtorCycleB"]) -> None:
        self.b = b


class CtorCycleB:
    def __init__(self, a: Autowired[CtorCycleA]) -> None:
        self.a = a


class MixedCycleA:
    def __init__(self, b: Autowired["MixedCycleB"]) -> None:
        self.b = b


class MixedCycleB:
    a: Autowired[MixedCycleA]


def test_registration_does_not_modify_the_class() -> None:
    """register() is a pure recording operation."""
    original_new = Service.__new__
    original_init = Service.__init__

    Container().register(Service)

    assert Service.__new__ is original_new
    assert Service.__init__ is original_init


def test_hand_written_instantiation_is_not_wired() -> None:
    """Cls() is plain Python: the container is not involved, so Autowired
    fields are absent. This is the deliberate semantic break."""
    container = Container()
    container.register(Dep)
    container.register(Service)

    manual = Service()

    assert not hasattr(manual, "dep")
    assert manual is not container.resolve(Service)


def test_unregistered_subclass_is_instantiable() -> None:
    """A subclass of a registered component is not itself a component and must
    behave like any other class."""

    class Child(Slotted):
        pass

    Container().register(Slotted)

    assert isinstance(Child(), Child)


def test_injected_fields_are_readable_inside_init() -> None:
    """Fields are set before __init__ runs. Nothing else in the suite covers
    this, so a silent reordering of the construction sequence would otherwise
    go unnoticed."""
    container = Container()
    container.register(Dep)
    container.register(ReadsFieldInInit)

    assert container.resolve(ReadsFieldInInit).seen == "Dep"


def test_slots_component_without_injected_fields_resolves() -> None:
    """No per-instance bookkeeping is written any more, so __slots__ is fine."""
    container = Container()
    container.register(Slotted)

    assert isinstance(container.resolve(Slotted), Slotted)


def test_slots_component_with_an_injected_field_fails_explicitly() -> None:
    """__slots__ is not statically decidable -- a base may supply __dict__ --
    so this is caught at injection time, not plan time."""

    class SlottedService:
        __slots__ = ()
        dep: Autowired[Dep]

    container = Container()
    container.register(Dep)
    container.register(SlottedService)

    with pytest.raises(UnconstructibleComponentError) as excinfo:
        container.resolve(SlottedService)

    assert "cannot be set" in str(excinfo.value)


def test_custom_new_requiring_arguments_fails_explicitly() -> None:
    container = Container()
    container.register(CustomNew)

    with pytest.raises(UnconstructibleComponentError) as excinfo:
        container.resolve(CustomNew)

    assert "__new__" in str(excinfo.value)


def test_new_that_resolves_during_construction_fails_explicitly() -> None:
    """Guards the "closed before its instance existed" branch. Without it the
    container would inject None instead of raising."""
    container = Container()
    container.register(ResolvesInNew)
    ResolvesInNew.container = container

    try:
        with pytest.raises(CircularDependencyError) as excinfo:
            container.resolve(ResolvesInNew)

        assert "__new__" in str(excinfo.value)
    finally:
        ResolvesInNew.container = None


def test_non_autowired_parameter_without_default_fails_explicitly() -> None:
    container = Container()
    container.register(NeedsArg)

    with pytest.raises(UnconstructibleComponentError) as excinfo:
        container.resolve(NeedsArg)

    assert "'url'" in str(excinfo.value)


def test_plan_failure_carries_the_resolution_chain() -> None:
    """Planning knows nothing about the stack, so the container re-raises its
    errors as copies carrying the chain -- see PyWireError.with_context."""

    class Host:
        dep: Autowired[NeedsArg]

    container = Container()
    container.register(NeedsArg)
    container.register(Host)

    with pytest.raises(UnconstructibleComponentError) as excinfo:
        container.resolve(Host)

    assert "Host -> NeedsArg" in str(excinfo.value) or "Host" in str(excinfo.value)


def test_missing_dependency_names_requester_and_chain() -> None:
    container = Container()
    container.register(Service)

    with pytest.raises(DependencyResolutionError) as excinfo:
        container.resolve(Service)

    message = str(excinfo.value)
    assert "Service.dep" in message
    assert "Service -> Dep" in message


@pytest.mark.parametrize("entry_point", [MixedCycleA, MixedCycleB])
def test_mixed_cycle_is_rejected_from_either_entry_point(
    entry_point: type,
) -> None:
    """A cycle with one constructor edge and one field edge must fail the same
    way whichever end you resolve from. Checking the current frame rather than
    the cycle would let one of the two entry points through."""
    container = Container()
    container.register(MixedCycleA)
    container.register(MixedCycleB)

    with pytest.raises(CircularDependencyError):
        container.resolve(entry_point)


def test_failed_resolution_leaves_no_partial_instance_behind() -> None:
    """Rollback must clear every bean built during the failed call, so a retry
    fails the same way instead of returning a broken object."""
    container = Container()
    container.register(CtorCycleA)
    container.register(CtorCycleB)

    for target in (CtorCycleA, CtorCycleA, CtorCycleB):
        with pytest.raises(CircularDependencyError):
            container.resolve(target)


def test_caught_inner_failure_leaves_no_partial_instance_behind() -> None:
    """A component that resolves an optional dependency inside try/except
    swallows the failure, so it never reaches an outer frame. Rollback has to
    fire per-subtree, or the partial objects stay cached forever.

    Asserted through the public surface: if Optional had stayed cached, the
    second resolve would hand back the half-built object instead of raising.
    """
    container = Container()

    class Optional:
        def __init__(self, missing: Autowired[Dep]) -> None:
            self.missing = missing

    class Host:
        def __init__(self) -> None:
            try:
                self.optional = container.resolve(Optional)
            except DependencyResolutionError:
                self.optional = None

    container.register(Optional)
    container.register(Host)

    assert container.resolve(Host).optional is None

    with pytest.raises(DependencyResolutionError):
        container.resolve(Optional)


def test_rollback_clears_ready_so_a_later_success_is_not_masked() -> None:
    """`ready` is what the unsynchronised fast path in resolve() trusts. If
    rollback cleared `instance` but left `ready` set, the fast path would keep
    handing out a disowned bean forever."""
    container = Container()

    class Flaky:
        attempts = 0

        def __init__(self) -> None:
            type(self).attempts += 1

            if type(self).attempts == 1:
                raise RuntimeError("first attempt fails")

    container.register(Flaky)

    with pytest.raises(RuntimeError):
        container.resolve(Flaky)

    assert isinstance(container.resolve(Flaky), Flaky)


def test_clear_instances_keeps_registrations() -> None:
    """The operation conftest relies on: drop the object graph, keep the
    registry, so a fresh resolve rebuilds rather than raising."""
    container = Container()
    container.register(Dep)
    container.register(Service)

    first = container.resolve(Service)
    container.clear_instances()
    second = container.resolve(Service)

    assert first is not second
    assert second.dep is not first.dep


class SlowDep:
    def __init__(self) -> None:
        # Widens the window in which two threads could both observe an unbuilt
        # definition, so the assertion below is exercising real contention.
        threading.Event().wait(0.005)


class SlowService:
    dep: Autowired[SlowDep]


def test_concurrent_resolution_is_serialised_into_one_instance() -> None:
    """FastAPI runs sync endpoints in a threadpool, so concurrent resolve() is
    a real scenario. The container serialises construction under one lock, so
    the guarantee under test is that contention cannot produce two singletons --
    not that a race is won, since the design admits none."""
    container = Container()
    container.register(SlowDep)
    container.register(SlowService)

    worker_count = 8
    barrier = threading.Barrier(worker_count)
    results: list[SlowService] = []
    results_lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        resolved = container.resolve(SlowService)

        with results_lock:
            results.append(resolved)

    threads = [threading.Thread(target=worker) for _ in range(worker_count)]

    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join()

    assert len(results) == worker_count
    assert len({id(result) for result in results}) == 1
    assert len({id(result.dep) for result in results}) == 1
```

Note on the local classes: `Optional`, `Host`, `SlottedService` and `Flaky` are local
because they close over `container` or need per-test state. `SlottedService` carries a
class-level `Autowired` annotation, which is resolved against *module* globals — that
works here only because `Dep` is module-level. Keep it that way.

- [ ] **Step 5: Run the new module**

Run: `uv run pytest tests/test_container_semantics.py -v`
Expected: all PASS.

- [ ] **Step 6: Run everything**

Run: `uv run pytest && uv run ruff check . && uv run pyright`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add tests/test_container_semantics.py tests/conftest.py tests/test_components.py
git commit -m "✅ Cover isolation, cycles, rollback and concurrency"
```

---

### Task 7: Documentation

**Files:**
- Modify: `README.md`, `CLAUDE.md`

- [ ] **Step 1: Fix the `README.md` Features list**

- Replace `- Explicit, independent containers` with:
  ```markdown
  - Explicit, independent containers — the same class registered in two
    containers yields two independent singletons
  ```
- Replace `- Circular dependency detection and handling` with:
  ```markdown
  - Circular dependencies through fields; a cycle passing through a
    constructor parameter is rejected with the dependency chain
  ```
- **Delete** `- BeanDefinition metadata for registered components` — it is no longer
  part of the public surface.
- Add `- Typed exception hierarchy rooted at PyWireError`.
- Fix line 3: `Python 3.12+` → `Python 3.13+`, matching `CLAUDE.md` and `ruff`'s target.

- [ ] **Step 2: Delete the manual-construction paragraph**

**Delete `README.md:81-84` entirely** — the paragraph beginning "Note that manually
constructing a registered component…". It does not merely describe the old behavior,
it sells it as a feature, and it is now the exact opposite of the truth.

Replace it, in the same place, with:

```markdown
Field injection and constructor injection can be used together on the same class, and
an injected field is set **before** your `__init__` body runs, so `__init__` can read
it. An explicit keyword argument passed to `container.resolve(...)` is not supported;
construct such objects yourself and register the instance.
```

- [ ] **Step 3: Add the wiring section**

Add after the Features list:

````markdown
## Components are wired by the container, not by the class

`register()` never modifies the class it registers. A component is wired only when the
container builds it:

```python
container.register(Repository)

wired = container.resolve(Repository)   # Autowired fields injected
unwired = Repository()                  # plain Python: no injection
```

`Repository()` is an ordinary object — its `Autowired` fields are absent, and reading
one raises `AttributeError`. Always go through `container.resolve(...)` (or
`container.get(...)`).

`container.clear_instances()` drops every cached singleton while keeping every
registration, for callers that want a fresh object graph.

### Inherited fields

An `Autowired` field declared on a base class is injected into every subclass the
container builds. Re-annotating it without `Autowired` opts out:

```python
class Base:
    repo: Autowired[Repo]

class Child(Base):
    repo: Repo          # not injected; Child supplies it some other way
```

### Cycles

Two components may depend on each other through **fields**. A cycle that passes
through a **constructor parameter** is rejected with `CircularDependencyError` and the
chain, whichever end you resolve from — a constructor argument has to exist before
`__init__` can be called, so such a cycle has no fixed point. Convert one dependency
from a constructor parameter to a field to express an intentional cycle.

One caveat: while a field cycle is being closed, the partner's `__init__` observes a
partially-wired object. Storing the reference is fine; calling methods on it from
inside `__init__` is not.

### Errors

Everything pywire raises derives from `PyWireError`:

| Error | Meaning |
|---|---|
| `RegistrationError` | The same class was registered twice in one container |
| `UnconstructibleComponentError` | The container can never build this class — `__new__` needs arguments, a parameter cannot be supplied, or an injected field cannot be set |
| `AnnotationResolutionError` | An `Autowired[...]` annotation names a type that cannot be resolved |
| `DependencyResolutionError` | A dependency is not registered, or failed to build |
| `CircularDependencyError` | A dependency cycle passes through a constructor parameter (a `DependencyResolutionError`) |

Messages carry the resolution chain and the member that asked for the dependency, so a
four-deep failure reads as one sentence.
````

- [ ] **Step 4: Update the `README.md` architecture tree**

Add `plans.py` and keep the tree in sync:

```
pywire/
├── container.py       # Registry and resolve-time construction
├── plans.py           # InjectionPlan: what a class needs
├── definitions.py     # BeanDefinition metadata
├── decorators.py      # @component decorator
├── exceptions.py      # Exception hierarchy
├── markers.py         # Autowired[T] marker and annotation evaluation
├── fastapi.py         # Optional FastAPI integration (wire())
└── __init__.py        # Public API
```

- [ ] **Step 5: Rewrite the `CLAUDE.md` injection sections**

Delete **both** "### Field injection mechanism (`Container._instrument`)" and
"### Constructor injection mechanism (`Container._instrument`)" and put this in their
place:

```markdown
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
10. set `definition.ready = True` — success path only; this is what the fast path reads
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
```

- [ ] **Step 6: Update the FastAPI section of `CLAUDE.md`**

The existing claim that "decorating a route with a bare `Autowired[T]` parameter never
fails" must stay true — add how:

```markdown
- `_wire_endpoint` reads annotations through `markers.callable_hints`, so an unrelated
  unresolvable annotation on *any* endpoint in the process can no longer abort route
  registration. When `resolve_autowired_type` raises for an `Autowired[T]` parameter,
  the parameter is still rewritten — to `annotation=object` with
  `Depends(_resolve_autowired_late(...))` — and the resolution is retried on the first
  request, memoised thereafter. That keeps decoration unconditionally safe *and* makes
  endpoints as lazy as components: a route may inject a service defined further down
  its own module. A genuinely undefined name fails at request time, naming the endpoint.
```

- [ ] **Step 7: Update the `CLAUDE.md` module table**

```markdown
| `container.py` | `Container`: registry, register/resolve/get/clear_instances, `_Resolution` (call-scoped stack + undo log), construction sequence, per-subtree rollback, lock |
| `plans.py` | `InjectionPlan.for_class()`: pure inspection of a class's Autowired fields and constructor parameters; `field_label`/`param_label`; rejects unconstructible classes |
| `definitions.py` | `BeanDefinition`: registration metadata, singleton slot, `ready` flag, cached `InjectionPlan` |
| `markers.py` | `Autowired[T]`, `evaluate_annotation()`, `callable_hints()`, `resolve_autowired_type()` |
| `exceptions.py` | Immutable `PyWireError` hierarchy with `with_context()` |
```

Delete the `Scope` / `PROTOTYPE` mention from the `definitions.py` row, and add a note
to the module table's preamble that `BeanDefinition` is no longer exported from
`pywire` — `Container.clear_instances()` covers what tests used it for.

- [ ] **Step 8: Verify the docs match reality**

Run: `uv run pytest`
Expected: PASS. Then:

```bash
grep -rn "_instrument\|_di_initializing\|_di_initialized\|is_autowired_annotation_text" src/
grep -rn "Scope\|PROTOTYPE" src/ README.md CLAUDE.md
```
Expected: no hits from either. The deleted-mechanism names are checked in `src/` only:
`CLAUDE.md` deliberately still mentions `_di_initializing`, once, to explain what the
redesign removed and why the same mistake must not reappear on `Container`.

- [ ] **Step 9: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "📝 Document resolve-time wiring and drop stale claims"
```

---

## Done criteria

- `uv run pytest`, `uv run ruff check .`, and `uv run pyright` all pass.
- `grep -rn "_instrument\|_di_initializing\|_di_initialized\|is_autowired_annotation_text" src/` returns nothing, and `grep -rn "Scope\|PROTOTYPE" src/ README.md CLAUDE.md` returns nothing.
- `from pywire import BeanDefinition` raises `ImportError`; `from pywire.definitions import BeanDefinition` works.
- `grep -rn "_registry" tests/` returns nothing — no test reaches into container internals (**D13**).
- Every commit in the series is a valid state of the library: check out each of the seven and run `uv run pytest` (**D11**).
- The version is still `0.3.1`; bumping to `0.4.0` awaits the user's explicit request.

## Follow-up (not this plan)

Spec 2: `register_instance` / `register_factory` and `as_type=` supertype binding —
the "push" primitive and Dependency Inversion support. Two error messages written in
Task 5 already point at `register_instance`; their wording is final and needs no change
when spec 2 lands.

Not in scope, noted while reviewing: `decorators._default_container` is lazily
initialised through an unguarded module-level global, so two threads racing the very
first `@component` could build two containers. Untouched here because `decorators.py` is
out of scope for this plan.
