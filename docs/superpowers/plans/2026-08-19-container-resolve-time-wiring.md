# Container resolve-time wiring — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `Container.register()` a pure registration operation that never mutates the registered class, and make `Container.resolve()` the single site where an instance is constructed and wired.

**Architecture:** Delete the `__new__`/`__init__` monkey-patching in `Container._instrument`. Move "what does this class need?" into a new `plans.py` (`InjectionPlan.for_class`), and put construction into `resolve()`: `cls.__new__(cls)` → early-register for field cycles → inject fields → resolve constructor args → `cls.__init__(...)`. "In construction" state moves from instance flags into a `Container._resolving` stack, which also produces the chain for cycle errors.

**Tech Stack:** Python 3.13, `uv`, pytest, ruff (`E, F, I, UP, RUF, ANN401`, line length 88, target `py313`), pyright (basic).

**Spec:** `docs/superpowers/specs/2026-08-18-container-resolve-time-wiring-design.md`

## Global Constraints

- Python `>=3.12` per `pyproject.toml`; ruff targets `py313`. Use PEP 695 syntax (`def register[T]`, `type X = ...`) as the existing code already does.
- Line length 88. Ruff rule sets `E, F, I, UP, RUF, ANN401` — in particular `ANN401` forbids bare `Any` in signatures without a `# noqa: ANN401` and a comment explaining why.
- All docstrings, comments, and exception messages in **English only**.
- Relative imports inside `src/pywire/` (`from .markers import ...`); absolute in tests (`from pywire import ...`).
- Package management is `uv`. Run tests with `uv run pytest`, lint with `uv run ruff check .`, type-check with `uv run pyright`.
- **Git in this repo requires an inline safe.directory flag.** Every git command below must be prefixed:
  `git -c safe.directory=C:/Users/alessio.gilardi/PycharmProjects/Personal/pywire ...`
  The plan writes plain `git` for readability; add the flag when running.
- Commit messages follow the project's gitmoji convention (`commit-moji` skill): `<emoji> <imperative description>`, max 72 chars, **no attribution trailers of any kind**.
- **Do not run `scripts/bump-version.sh`.** The version bump to `0.4.0` happens only on the user's explicit request, after this plan is complete.
- `decorators.py` is not modified by any task.

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `src/pywire/exceptions.py` | Modify | Exception hierarchy rooted at `PyWireError` |
| `src/pywire/plans.py` | **Create** | `InjectionPlan`: what a class needs, computed by pure inspection |
| `src/pywire/markers.py` | Modify | `Autowired`; `resolve_autowired_type` gains a `context` argument and raises instead of returning `None` on an unresolvable forward reference |
| `src/pywire/definitions.py` | Modify | `BeanDefinition` gains `plan`, loses `scope`; `Scope` deleted |
| `src/pywire/container.py` | Rewrite | `Container`: registry, resolution stack, construction sequence |
| `src/pywire/fastapi.py` | Modify | Pass endpoint context to `resolve_autowired_type` |
| `src/pywire/__init__.py` | Modify | Export the new exception names |
| `tests/test_exceptions.py` | **Create** | Hierarchy assertions |
| `tests/test_plans.py` | **Create** | `InjectionPlan.for_class` in isolation |
| `tests/test_container_semantics.py` | **Create** | Isolation, non-invasiveness, edge cases, rollback, threads |
| `tests/conftest.py` | Modify | `autouse` fixture resetting the default container |
| `tests/test_components.py` | Modify | Real isolation test; `RegistrationError` |
| `tests/test_constructor_injection.py` | Modify | Constructor cycle now raises; collapse duplicated settings classes |
| `tests/test_markers.py` | Modify | Unresolvable forward reference raises |
| `tests/test_fastapi_integration.py` | Modify | One added test for endpoint annotation context |
| `README.md`, `CLAUDE.md` | Modify | Documentation |

**Preserved limitation (not in scope):** `inspect.get_annotations(cls)` returns only the class's *own* annotations, so `Autowired` fields declared on a base class are not injected into a subclass. This is today's behavior and stays unchanged.

---

### Task 1: Exception hierarchy

**Files:**
- Modify: `src/pywire/exceptions.py` (whole file, currently 2 lines)
- Modify: `src/pywire/__init__.py:11,14-25`
- Test: `tests/test_exceptions.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `PyWireError(Exception)`, `RegistrationError(PyWireError)`, `AnnotationResolutionError(PyWireError)`, `DependencyResolutionError(PyWireError)`, `CircularDependencyError(DependencyResolutionError)`. All importable from `pywire`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_exceptions.py`:

```python
"""Tests for the pywire exception hierarchy."""

from __future__ import annotations

from pywire import (
    AnnotationResolutionError,
    CircularDependencyError,
    DependencyResolutionError,
    PyWireError,
    RegistrationError,
)


def test_every_error_derives_from_pywire_error() -> None:
    """A caller can catch PyWireError to handle any pywire failure."""
    for error in (
        RegistrationError,
        AnnotationResolutionError,
        DependencyResolutionError,
        CircularDependencyError,
    ):
        assert issubclass(error, PyWireError)


def test_circular_dependency_error_is_a_resolution_error() -> None:
    """A circular dependency is a resolution failure, so catching
    DependencyResolutionError must also catch it."""
    assert issubclass(CircularDependencyError, DependencyResolutionError)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_exceptions.py -v`
Expected: FAIL — `ImportError: cannot import name 'PyWireError' from 'pywire'`

- [ ] **Step 3: Write the implementation**

Replace the whole of `src/pywire/exceptions.py`:

```python
class PyWireError(Exception):
    """Base class for every error raised by pywire."""


class RegistrationError(PyWireError):
    """Raised when a component cannot be registered."""


class AnnotationResolutionError(PyWireError):
    """Raised when an Autowired[...] annotation cannot be resolved to a type."""


class DependencyResolutionError(PyWireError):
    """Raised when a dependency cannot be resolved or constructed."""


class CircularDependencyError(DependencyResolutionError):
    """Raised when constructor parameters form a dependency cycle."""
```

In `src/pywire/__init__.py`, replace the `exceptions` import and `__all__`:

```python
from .exceptions import (
    AnnotationResolutionError,
    CircularDependencyError,
    DependencyResolutionError,
    PyWireError,
    RegistrationError,
)
```

```python
__all__ = [
    "AnnotationResolutionError",
    "Autowired",
    "BeanDefinition",
    "CircularDependencyError",
    "Container",
    "DependencyResolutionError",
    "PyWireError",
    "RegistrationError",
    "agent",
    "client",
    "component",
    "get_default_container",
    "repository",
    "service",
]
```

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest && uv run ruff check . && uv run pyright`
Expected: all PASS. Nothing consumes the new names yet, so no existing behavior changes.

- [ ] **Step 5: Commit**

```bash
git add src/pywire/exceptions.py src/pywire/__init__.py tests/test_exceptions.py
git commit -m "✨ Add PyWireError exception hierarchy"
```

---

### Task 2: Extract `InjectionPlan`

Pure addition. `container.py` is untouched and keeps its own copy of the logic; Task 3 switches it over. Splitting it this way is what makes Task 5's diff readable.

**Files:**
- Create: `src/pywire/plans.py`
- Test: `tests/test_plans.py` (create)

**Interfaces:**
- Consumes: `resolve_autowired_type` from `markers.py` (current signature, still returns `None` on an unresolvable forward reference — Task 4 changes that).
- Produces: `InjectionPlan` — a frozen slotted dataclass with `fields: dict[str, type]`, `ctor_params: dict[str, type]`, `unsatisfiable: tuple[str, ...]`, and the classmethod `InjectionPlan.for_class(target: type) -> InjectionPlan`.

`unsatisfiable` holds the names of `__init__` parameters the container cannot supply: not `Autowired`, and without a default. The plan only *records* them; `Container` decides what to do (Task 5). `*args`/`**kwargs` are skipped and never counted as unsatisfiable.

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


class NeedsPlainArg:
    def __init__(self, url: str) -> None:
        self.url = url


class PlainArgWithDefault:
    def __init__(self, url: str = "sqlite://memory") -> None:
        self.url = url


class VariadicOnly:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.args = args


def test_plans_autowired_fields() -> None:
    plan = InjectionPlan.for_class(FieldOnly)

    assert plan.fields == {"dep": Dep}
    assert plan.ctor_params == {}
    assert plan.unsatisfiable == ()


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


def test_non_autowired_parameter_without_default_is_unsatisfiable() -> None:
    plan = InjectionPlan.for_class(NeedsPlainArg)

    assert plan.unsatisfiable == ("url",)
    assert plan.ctor_params == {}


def test_non_autowired_parameter_with_default_is_ignored() -> None:
    plan = InjectionPlan.for_class(PlainArgWithDefault)

    assert plan.unsatisfiable == ()
    assert plan.ctor_params == {}


def test_variadic_parameters_are_never_unsatisfiable() -> None:
    """*args/**kwargs have no default but the container never needs to
    supply them."""
    plan = InjectionPlan.for_class(VariadicOnly)

    assert plan.unsatisfiable == ()


def test_class_without_init_or_annotations_plans_to_nothing() -> None:
    class Bare:
        pass

    plan = InjectionPlan.for_class(Bare)

    assert plan.fields == {}
    assert plan.ctor_params == {}
    assert plan.unsatisfiable == ()


def test_unrelated_unresolvable_annotation_does_not_break_planning() -> None:
    """A class-level annotation that cannot be evaluated must not prevent
    the Autowired fields on the same class from being planned."""

    class Mixed:
        dep: Autowired[Dep]
        broken: "NeverDefinedAnywhere"  # noqa: F821

    plan = InjectionPlan.for_class(Mixed)

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

- [ ] **Step 3: Write the implementation**

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


@dataclass(frozen=True, slots=True)
class InjectionPlan:
    """What a class needs in order to be constructed by a Container.

    Planning is pure inspection: it reads annotations and signatures and
    never instantiates, registers, or resolves anything.

    Attributes:
        fields: Class-level Autowired fields, mapped to the type to inject.
        ctor_params: __init__ parameters annotated Autowired, mapped to the
            type to inject.
        unsatisfiable: Names of __init__ parameters the container cannot
            supply: neither Autowired nor defaulted. Variadic parameters are
            excluded. Recording them here keeps the plan descriptive; the
            Container decides whether that makes the class unusable.
    """

    fields: dict[str, type]
    ctor_params: dict[str, type]
    unsatisfiable: tuple[str, ...]

    @classmethod
    def for_class(cls, target: type) -> InjectionPlan:
        """Inspect target and describe everything the container must supply."""
        module_globals = vars(sys.modules[target.__module__])
        ctor_params, unsatisfiable = _plan_constructor(target, module_globals)

        return cls(
            fields=_plan_fields(target, module_globals),
            ctor_params=ctor_params,
            unsatisfiable=unsatisfiable,
        )


def _plan_fields(
    target: type,
    module_globals: dict[str, object],
) -> dict[str, type]:
    """Collect the class-level Autowired fields declared on target itself.

    Annotations are evaluated one at a time rather than through
    get_annotations(eval_str=True): a single unevaluable annotation
    (a TYPE_CHECKING-only import, a name defined nowhere) would otherwise
    raise NameError and discard the whole class's plan, including its
    perfectly valid Autowired fields.
    """
    fields: dict[str, type] = {}

    for name, annotation in inspect.get_annotations(target).items():
        evaluated = _evaluate(annotation, module_globals)

        if evaluated is None:
            continue

        field_type = resolve_autowired_type(evaluated, module_globals)

        if field_type is not None:
            fields[name] = field_type

    return fields


def _plan_constructor(
    target: type,
    module_globals: dict[str, object],
) -> tuple[dict[str, type], tuple[str, ...]]:
    """Split __init__'s parameters into Autowired ones and unsatisfiable ones."""
    original_init = target.__init__

    if original_init is object.__init__:
        return {}, ()

    hints = _init_hints(original_init)
    parameters = list(inspect.signature(original_init).parameters.values())
    ctor_params: dict[str, type] = {}
    unsatisfiable: list[str] = []

    # Skip "self", the first parameter of an instance __init__.
    for parameter in parameters[1:]:
        if parameter.kind in _VARIADIC_KINDS:
            continue

        param_type = resolve_autowired_type(
            hints.get(parameter.name), module_globals
        )

        if param_type is not None:
            ctor_params[parameter.name] = param_type
        elif parameter.default is inspect.Parameter.empty:
            unsatisfiable.append(parameter.name)

    return ctor_params, tuple(unsatisfiable)


def _init_hints(original_init: Any) -> dict[str, Any]:  # noqa: ANN401
    """Resolve __init__'s annotations, tolerating unresolvable ones.

    No explicit globalns is passed: original_init may be inherited from a
    base class defined in a different module, so the subclass's module would
    be the wrong resolution context. get_type_hints() reads
    original_init.__globals__ internally, which is always the module
    __init__ was defined in.

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


def _evaluate(
    annotation: object,
    module_globals: dict[str, object],
) -> Any | None:  # noqa: ANN401
    """Evaluate a possibly-stringified annotation, or None if it cannot be.

    Returns Any because it passes through whatever eval() produced, exactly
    as the annotation machinery it feeds does.
    """
    if not isinstance(annotation, str):
        return annotation

    try:
        return eval(annotation, module_globals)  # noqa: S307
    except NameError:
        return None
```

Note: `# noqa: S307` is harmless if the `S` rule set is not enabled; keep it so enabling bandit rules later does not break the file.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_plans.py -v`
Expected: all PASS.

- [ ] **Step 5: Run the full suite and checks**

Run: `uv run pytest && uv run ruff check . && uv run pyright`
Expected: all PASS — `container.py` is untouched, so no behavior changed.

- [ ] **Step 6: Commit**

```bash
git add src/pywire/plans.py tests/test_plans.py
git commit -m "✨ Add InjectionPlan describing a class's dependencies"
```

---

### Task 3: Make `Container._instrument` use the plan

Pure refactor: the instrumentation stays, only the source of "what to inject" changes. The whole existing suite must stay green — that is the point of this task, and it is what validates `plans.py` against every case the suite already covers.

**Files:**
- Modify: `src/pywire/container.py:59-103` (delete the inline planning, call `InjectionPlan.for_class`), `:153-175` (use the plan)

**Interfaces:**
- Consumes: `InjectionPlan.for_class` from Task 2.
- Produces: no API change.

- [ ] **Step 1: Replace the planning block**

In `src/pywire/container.py`, add the import:

```python
from .plans import InjectionPlan
```

Then in `_instrument`, delete everything from `raw_annotations = ...` (line 62) through the `ctor_autowired_params = {...}` block (line 103) **except** the two `original_*` captures, and replace with:

```python
    def _instrument(self, cls: type) -> None:
        """Install the __new__ and __init__ needed for field injection."""

        plan = InjectionPlan.for_class(cls)
        original_init = cls.__init__
        original_new: Any = cls.__new__
```

The now-unused imports `inspect`, `sys`, `get_type_hints` and the `resolve_autowired_type` import must be removed from `container.py`; ruff's `F401` will flag any that are left.

- [ ] **Step 2: Replace the two injection loops**

In the nested `init`, replace the field loop:

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

`plan.unsatisfiable` is deliberately unused in this task; `Container` starts acting on it in Task 5.

- [ ] **Step 3: Run the full suite**

Run: `uv run pytest -v`
Expected: all 40 tests PASS, unchanged. If any test fails, `InjectionPlan` differs from the logic it replaced — fix `plans.py`, not the test.

- [ ] **Step 4: Lint and type-check**

Run: `uv run ruff check . && uv run pyright`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/pywire/container.py
git commit -m "♻️ Source injection metadata from InjectionPlan"
```

---

### Task 4: Stop swallowing unresolvable annotations

**Files:**
- Modify: `src/pywire/markers.py:29-57`
- Modify: `src/pywire/plans.py` (pass context)
- Modify: `src/pywire/fastapi.py:46`
- Test: `tests/test_markers.py`, `tests/test_plans.py`, `tests/test_fastapi_integration.py`

**Interfaces:**
- Consumes: `AnnotationResolutionError` from Task 1.
- Produces: `resolve_autowired_type(annotation, module_globals, context: str | None = None)`. Returns `None` **only** when the annotation is not `Autowired[...]`. Raises `AnnotationResolutionError` when it is `Autowired["X"]` and `X` cannot be evaluated. The optional `context` is interpolated into the message; callers that know the owner pass it.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_markers.py` (it already imports `Autowired` and
`get_type_hints`; add only `pytest`, `AnnotationResolutionError`, and
`resolve_autowired_type`):

```python
def test_unresolvable_forward_reference_raises() -> None:
    """Autowired["Missing"] is a broken annotation, not an absent one: it
    must fail loudly instead of silently skipping injection."""

    class Component:
        dependency: Autowired["Missing"]  # noqa: F821

    hints = get_type_hints(Component, include_extras=True)

    with pytest.raises(AnnotationResolutionError) as excinfo:
        resolve_autowired_type(hints["dependency"], {"__name__": "tests.fake"})

    assert "Missing" in str(excinfo.value)


def test_non_autowired_annotation_still_returns_none() -> None:
    """A plain annotation is not an error; it is simply not injected."""
    assert resolve_autowired_type(int, {}) is None
```

Add to that file's imports:

```python
import pytest

from pywire import AnnotationResolutionError
from pywire.markers import resolve_autowired_type
```

Append to `tests/test_plans.py` (it already imports `Autowired` and
`InjectionPlan`; add only `pytest` and `AnnotationResolutionError`):

```python
def test_unresolvable_autowired_field_names_the_owner() -> None:
    """The error must say which class and field carry the broken annotation."""

    class Broken:
        dep: Autowired["NotAThing"]  # noqa: F821

    with pytest.raises(AnnotationResolutionError) as excinfo:
        InjectionPlan.for_class(Broken)

    message = str(excinfo.value)
    assert "NotAThing" in message
    assert "Broken.dep" in message
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_markers.py tests/test_plans.py -v`
Expected: FAIL — the new tests fail because `resolve_autowired_type` currently returns `None`.

- [ ] **Step 3: Change `markers.py`**

Replace `resolve_autowired_type` in `src/pywire/markers.py`:

```python
def resolve_autowired_type(
    annotation: object,
    module_globals: dict[str, object],
    context: str | None = None,
) -> Any | None:  # noqa: ANN401
    """Return the wrapped type if annotation is Autowired[T], else None.

    Args:
        annotation: The annotation to inspect.
        module_globals: Globals of the module a forward reference is
            evaluated against.
        context: Optional "Owner.member" label, interpolated into the error
            message so a broken annotation names the code that carries it.

    Returns:
        T if annotation is Autowired[T]; None if it is not an Autowired
        annotation at all.

    Raises:
        AnnotationResolutionError: annotation is Autowired["X"] and X cannot
            be evaluated. Returning None here would be indistinguishable
            from "not Autowired", silently skipping the injection.

    The return type stays Any: it passes through whatever get_args()
    extracted, unexamined, and typeshed's own get_args() is Any-returning.
    """
    if get_origin(annotation) is not Autowired:
        return None

    (wrapped,) = get_args(annotation)

    # Forward references inside Autowired["X"] are left unresolved by
    # eval_str=True, since it only evaluates the outer annotation string.
    # Under the PEP 695 alias, such a reference surfaces as a plain str
    # rather than a ForwardRef.
    if isinstance(wrapped, str):
        try:
            return eval(wrapped, module_globals)  # noqa: S307
        except NameError as exc:
            where = f" on {context!r}" if context else ""
            module_name = module_globals.get("__name__", "<unknown>")
            raise AnnotationResolutionError(
                f'Cannot resolve annotation Autowired["{wrapped}"]{where}: '
                f"name {wrapped!r} is not defined in module {module_name!r}."
            ) from exc

    return wrapped
```

Add to the imports at the top of `markers.py`:

```python
from .exceptions import AnnotationResolutionError
```

- [ ] **Step 4: Pass context from `plans.py`**

In `_plan_fields`, change the call:

```python
        field_type = resolve_autowired_type(
            evaluated, module_globals, context=f"{target.__qualname__}.{name}"
        )
```

In `_plan_constructor`:

```python
        param_type = resolve_autowired_type(
            hints.get(parameter.name),
            module_globals,
            context=f"{target.__qualname__}.__init__({parameter.name})",
        )
```

- [ ] **Step 5: Pass context from `fastapi.py`**

In `_wire_endpoint`, replace line 46:

```python
        target = resolve_autowired_type(
            hints.get(name),
            module_globals,
            context=f"{func.__qualname__}({name})",
        )
```

- [ ] **Step 6: Add the FastAPI test**

Append to `tests/test_fastapi_integration.py`:

```python
def test_unresolvable_autowired_endpoint_parameter_names_the_endpoint() -> None:
    """A broken Autowired annotation on a route must fail with a pywire
    error naming the endpoint, not an opaque FastAPIError."""
    router = APIRouter()

    with pytest.raises(AnnotationResolutionError) as excinfo:

        @router.get("/broken")
        def broken(service: Autowired["NoSuchService"]) -> dict[str, str]:  # noqa: F821
            return {"ok": "yes"}

    assert "broken" in str(excinfo.value)
```

Ensure `AnnotationResolutionError`, `Autowired`, `APIRouter`, and `pytest` are imported in that file (`APIRouter` and `pytest` already are).

- [ ] **Step 7: Run everything**

Run: `uv run pytest && uv run ruff check . && uv run pyright`
Expected: all PASS. If an existing test now raises `AnnotationResolutionError`, it was relying on a silently skipped injection — report it rather than papering over it.

- [ ] **Step 8: Commit**

```bash
git add src/pywire/markers.py src/pywire/plans.py src/pywire/fastapi.py \
        tests/test_markers.py tests/test_plans.py tests/test_fastapi_integration.py
git commit -m "🐛 Raise instead of silently skipping broken Autowired refs"
```

---

### Task 5: ⚠️ The rewrite — resolve-time construction

**This is the point of no return.** The old and new mechanisms cannot coexist on the same class, so `container.py` and `definitions.py` change together and the suite goes red in the middle of this task on purpose.

**Two deliberate widenings of the spec's error-message table.** Both are supersets of
the specified text, carrying the same information — noted here so a reviewer does not
read them as drift:

- The frozen-dataclass row becomes *"the attribute cannot be set (frozen dataclass, or
  `__slots__` without this field)"*. The same `AttributeError` is raised by a
  `__slots__` class that declares an `Autowired` field with no slot for it, and naming
  only "frozen" would misdiagnose that case.
- The requester-and-chain form of the "not registered" message is produced by
  `Container._require_registered`, i.e. only for a dependency reached *through* another
  bean. A direct `container.resolve(Missing)` has no requester and no chain, so it
  keeps the shorter existing message.

**Files:**
- Rewrite: `src/pywire/container.py`
- Modify: `src/pywire/definitions.py` (whole file)
- Modify: `tests/test_components.py:113-125`, `tests/test_constructor_injection.py:93-118,148-163`

**Interfaces:**
- Consumes: `InjectionPlan` (Task 2), the exception hierarchy (Task 1).
- Produces: unchanged public signatures — `Container.register[T](cls: type[T]) -> type[T]`, `Container.resolve[T](target_type: type[T]) -> T`, `Container.get[T](target_type: type[T]) -> T`. `BeanDefinition(cls, instance=None, plan=None)`. `Scope` no longer exists.

- [ ] **Step 1: Update the two tests whose semantics change**

In `tests/test_components.py`, `test_duplicate_registration_raises_error`: replace `pytest.raises(ValueError)` with `pytest.raises(RegistrationError)` and import `RegistrationError` from `pywire`.

In `tests/test_constructor_injection.py`, replace the circular test (currently at `:148-163`) with:

```python
def test_circular_constructor_dependencies_raise_with_the_chain():
    """A cycle through constructor parameters cannot hand over a usable
    argument, so it fails naming the chain instead of injecting a
    half-constructed partner."""
    container = Container()

    container.register(CircularA)
    container.register(CircularB)

    with pytest.raises(CircularDependencyError) as excinfo:
        container.resolve(CircularA)

    message = str(excinfo.value)
    assert "CircularA -> CircularB -> CircularA" in message
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

Update both tests that used `AppSettingsWithEnv`/`AppSettingsDefault` and `SettingsConsumerWithEnv`/`SettingsConsumerDefault` to use `AppSettings`/`SettingsConsumer`. The duplication existed only to dodge the cross-container leak this task removes.

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
        instance: The singleton, once created. Set before __init__ runs, so
            a dependency cycle closing through a field finds it.
        plan: Cached InjectionPlan, computed on first resolution rather than
            at registration time — a forward reference to a class defined
            later in the module can only be resolved late.
    """

    cls: type
    instance: object | None = None
    plan: InjectionPlan | None = None
```

`Scope` is deleted. It is not exported from `__init__.py`, so no export changes.

- [ ] **Step 4: Rewrite `container.py`**

Replace the whole file:

```python
from __future__ import annotations

import threading
from typing import cast

from .definitions import BeanDefinition
from .exceptions import (
    CircularDependencyError,
    DependencyResolutionError,
    RegistrationError,
)
from .plans import InjectionPlan

type Registry = dict[type, BeanDefinition]


class Container:
    """Dependency Injection container.

    register() records; resolve() constructs. A registered class is never
    modified, so each Container owns a genuinely independent singleton
    scope and a hand-written Cls() stays plain Python -- unwired.
    """

    def __init__(self) -> None:
        self._registry: Registry = dict()
        # Reentrant: resolve() recurses into itself for every dependency.
        # Held across the whole call, it also keeps _resolving and _created
        # single-threaded, which per-type locks could not do without
        # risking deadlock on cycles.
        self._lock = threading.RLock()
        self._resolving: list[type] = []
        self._created: list[BeanDefinition] = []

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
        """Return the singleton associated with target_type, building it
        (and everything it needs) on first call."""
        with self._lock:
            definition = self._registry.get(target_type)

            if definition is None:
                name = getattr(target_type, "__name__", target_type)
                raise DependencyResolutionError(
                    f"Cannot resolve '{name}': it is not registered "
                    "in the container."
                )

            if definition.instance is not None:
                # Already built, or a cycle closing through a field: the
                # partner is still under construction but its identity is
                # final, which is all a stored reference needs.
                return cast(T, definition.instance)

            return cast(T, self._create(target_type, definition))

    def get[T](self, target_type: type[T]) -> T:
        """Readable alias for resolve()."""
        return self.resolve(target_type)

    def _create(self, target_type: type, definition: BeanDefinition) -> object:
        """Build, wire, and initialize a single bean."""
        resolving_depth = len(self._resolving)
        created_depth = len(self._created)
        self._resolving.append(target_type)

        try:
            instance = self._instantiate(target_type)

            # Early registration: a dependency cycle closing through a field
            # finds this instance instead of recursing forever.
            definition.instance = instance
            self._created.append(definition)

            if definition.plan is None:
                definition.plan = InjectionPlan.for_class(target_type)

            self._inject_fields(instance, definition.plan, target_type)
            kwargs = self._resolve_ctor_args(definition.plan, target_type)
            target_type.__init__(instance, **kwargs)

            return instance
        except BaseException:
            self._roll_back(created_depth)
            raise
        finally:
            del self._resolving[resolving_depth:]

            if not self._resolving:
                self._created.clear()

    def _roll_back(self, created_depth: int) -> None:
        """Discard every instance built during the failed call.

        Clearing only the failing bean is not enough: a partner already
        initialized during the same call may hold a reference to a
        half-built object, which would leave the registry handing out an
        instance the container has otherwise disowned.
        """
        for definition in self._created[created_depth:]:
            definition.instance = None

        del self._created[created_depth:]

    def _instantiate(self, target_type: type) -> object:
        """Allocate the instance without running __init__."""
        try:
            return target_type.__new__(target_type)
        except TypeError as exc:
            raise DependencyResolutionError(
                f"Cannot construct '{target_type.__name__}': its __new__ "
                "requires arguments. Register a pre-built instance instead."
            ) from exc

    def _inject_fields(
        self,
        instance: object,
        plan: InjectionPlan,
        target_type: type,
    ) -> None:
        """Set every planned Autowired field on the fresh instance."""
        for name, field_type in plan.fields.items():
            self._require_registered(field_type, f"{target_type.__name__}.{name}")
            value = self.resolve(field_type)

            try:
                setattr(instance, name, value)
            except AttributeError as exc:
                raise DependencyResolutionError(
                    f"Cannot inject field '{name}' into "
                    f"'{target_type.__name__}': the attribute cannot be set "
                    "(frozen dataclass, or __slots__ without this field). "
                    "Use constructor injection instead."
                ) from exc

    def _resolve_ctor_args(
        self,
        plan: InjectionPlan,
        target_type: type,
    ) -> dict[str, object]:
        """Resolve the Autowired constructor parameters, refusing cycles."""
        if plan.unsatisfiable:
            name = plan.unsatisfiable[0]
            raise DependencyResolutionError(
                f"Cannot construct '{target_type.__name__}': parameter "
                f"'{name}' has no default and is not Autowired. Register an "
                "instance instead, or give it a default."
            )

        kwargs: dict[str, object] = {}

        for name, dep_type in plan.ctor_params.items():
            if dep_type in self._resolving:
                raise CircularDependencyError(
                    "Circular dependency through constructor parameters: "
                    f"{self._chain(dep_type)}. Convert one of these "
                    "dependencies to a field to allow the cycle."
                )

            self._require_registered(
                dep_type, f"{target_type.__name__}.__init__({name})"
            )
            kwargs[name] = self.resolve(dep_type)

        return kwargs

    def _require_registered(self, dep_type: type, requester: str) -> None:
        """Fail with the requester and the chain, not just the missing type."""
        if dep_type in self._registry:
            return

        chain = " -> ".join(current.__name__ for current in self._resolving)
        raise DependencyResolutionError(
            f"Cannot resolve '{dep_type.__name__}' required by "
            f"'{requester}': not registered in this container. "
            f"Resolution chain: {chain} -> {dep_type.__name__}"
        )

    def _chain(self, dep_type: type) -> str:
        """Render the cycle starting at its first occurrence on the stack."""
        start = self._resolving.index(dep_type)
        names = [current.__name__ for current in self._resolving[start:]]

        return " -> ".join([*names, dep_type.__name__])
```

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -v`
Expected: all PASS, including the three unchanged tests in `test_circular_dependencies.py` (field cycles stay legal) and all 12 in `test_fastapi_integration.py`. A failure in either file is a regression, not an intended change — fix `container.py`, not the test.

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

### Task 6: Tests for the guarantees the rewrite creates

Additive. These encode the properties Task 5 made possible, which no existing test could express.

**Files:**
- Create: `tests/test_container_semantics.py`
- Modify: `tests/conftest.py`
- Modify: `tests/test_components.py:78-98`

**Interfaces:**
- Consumes: everything from Task 5.
- Produces: no source changes.

- [ ] **Step 1: Add the default-container reset fixture**

Replace `tests/conftest.py`:

```python
"""Pytest configuration and fixtures."""

from collections.abc import Iterator

import pytest

from pywire import decorators


@pytest.fixture(autouse=True)
def reset_default_container() -> Iterator[None]:
    """Isolate tests that register on the module-level default container.

    @component writes into a process-wide container that is never reset, so
    without this fixture tests contaminate each other invisibly and
    order-dependently.
    """
    decorators._default_container = None
    yield
    decorators._default_container = None
```

- [ ] **Step 2: Turn the fake isolation test into a real one**

In `tests/test_components.py`, replace `test_multiple_containers_independent` (`:78-98`) with:

```python
def test_multiple_containers_independent():
    """The same class registered in two containers must yield two distinct
    singletons, along with two distinct dependency graphs.

    The previous version of this test registered *different* classes in the
    two containers, so its assertion was true by construction.
    """

    class SharedDep:
        pass

    class SharedService:
        dep: Autowired[SharedDep]

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

- [ ] **Step 3: Run it to verify it passes**

Run: `uv run pytest tests/test_components.py -v`
Expected: PASS. (Before Task 5 this test would have failed — that is what makes it worth writing.)

- [ ] **Step 4: Write the semantics test module**

Create `tests/test_container_semantics.py`:

```python
"""Tests for the guarantees resolve-time wiring establishes.

Each of these describes something the previous class-instrumentation
mechanism made impossible, so together they are the regression net for the
redesign.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

import pytest

from pywire import (
    Autowired,
    CircularDependencyError,
    Container,
    DependencyResolutionError,
)


class Dep:
    pass


class Service:
    dep: Autowired[Dep]


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
    """A subclass of a registered component is not itself a component and
    must behave like any other class."""

    class Base:
        pass

    class Child(Base):
        pass

    Container().register(Base)

    assert isinstance(Child(), Child)


def test_slots_component_without_injected_fields_resolves() -> None:
    """No per-instance bookkeeping is written any more, so __slots__ is fine."""

    class Slotted:
        __slots__ = ()

    container = Container()
    container.register(Slotted)

    assert isinstance(container.resolve(Slotted), Slotted)


def test_frozen_dataclass_with_injected_field_fails_explicitly() -> None:
    @dataclass(frozen=True)
    class FrozenService:
        dep: Autowired[Dep]

    container = Container()
    container.register(Dep)
    container.register(FrozenService)

    with pytest.raises(DependencyResolutionError) as excinfo:
        container.resolve(FrozenService)

    assert "cannot be set" in str(excinfo.value)


def test_custom_new_requiring_arguments_fails_explicitly() -> None:
    class CustomNew:
        def __new__(cls, token: str) -> CustomNew:
            return super().__new__(cls)

        def __init__(self, token: str) -> None:
            self.token = token

    container = Container()
    container.register(CustomNew)

    with pytest.raises(DependencyResolutionError) as excinfo:
        container.resolve(CustomNew)

    assert "__new__" in str(excinfo.value)


def test_non_autowired_parameter_without_default_fails_explicitly() -> None:
    class NeedsArg:
        def __init__(self, url: str) -> None:
            self.url = url

    container = Container()
    container.register(NeedsArg)

    with pytest.raises(DependencyResolutionError) as excinfo:
        container.resolve(NeedsArg)

    assert "'url'" in str(excinfo.value)


def test_missing_dependency_names_requester_and_chain() -> None:
    container = Container()
    container.register(Service)

    with pytest.raises(DependencyResolutionError) as excinfo:
        container.resolve(Service)

    message = str(excinfo.value)
    assert "Service.dep" in message
    assert "Service -> Dep" in message


class CycleA:
    def __init__(self, b: Autowired["CycleB"]) -> None:
        self.b = b


class CycleB:
    def __init__(self, a: Autowired[CycleA]) -> None:
        self.a = a


def test_failed_resolution_leaves_no_partial_instance_behind() -> None:
    """Rollback must clear every bean built during the failed call, so a
    retry fails the same way instead of returning a broken object."""
    container = Container()
    container.register(CycleA)
    container.register(CycleB)

    with pytest.raises(CircularDependencyError):
        container.resolve(CycleA)

    with pytest.raises(CircularDependencyError):
        container.resolve(CycleA)

    with pytest.raises(CircularDependencyError):
        container.resolve(CycleB)


class SlowDep:
    def __init__(self) -> None:
        # Widens the window in which two threads can both observe an
        # unbuilt definition; without it the race practically never occurs.
        threading.Event().wait(0.005)


class SlowService:
    dep: Autowired[SlowDep]


def test_concurrent_resolution_builds_one_instance() -> None:
    """FastAPI runs sync endpoints in a threadpool, so concurrent resolve()
    is a real scenario. Probabilistic by nature; the barrier is what makes
    the collision likely enough to be worth asserting on."""
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

- [ ] **Step 5: Run the new module**

Run: `uv run pytest tests/test_container_semantics.py -v`
Expected: all PASS.

- [ ] **Step 6: Run everything**

Run: `uv run pytest && uv run ruff check . && uv run pyright`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add tests/test_container_semantics.py tests/conftest.py tests/test_components.py
git commit -m "✅ Cover isolation, non-invasiveness, rollback and threads"
```

---

### Task 7: Documentation

**Files:**
- Modify: `README.md` (Features list, plus a new subsection)
- Modify: `CLAUDE.md` ("Field injection mechanism", "Constructor injection mechanism", module table)

**Interfaces:**
- Consumes: the finished implementation.
- Produces: no code.

- [ ] **Step 1: Update `README.md`**

In the Features list, replace `- Explicit, independent containers` with:

```markdown
- Explicit, independent containers — the same class registered in two
  containers yields two independent singletons
```

Replace `- Circular dependency detection and handling` with:

```markdown
- Circular dependencies through fields; cycles through constructor
  parameters are rejected with the dependency chain
```

Add after the Features list:

```markdown
## Components are wired by the container, not by the class

`register()` never modifies the class it registers. A component is wired
only when the container builds it:

```python
container.register(Repository)

wired = container.resolve(Repository)   # Autowired fields injected
unwired = Repository()                  # plain Python: no injection
```

`Repository()` is an ordinary object — its `Autowired` fields are absent, and
reading one raises `AttributeError`. Always go through
`container.resolve(...)` (or `container.get(...)`).
```

- [ ] **Step 2: Update `CLAUDE.md`**

Delete the whole "### Field injection mechanism (`Container._instrument`)" and
"### Constructor injection mechanism (`Container._instrument`)" sections, and put this
in their place:

```markdown
### Injection mechanism (`Container.resolve`)

`register()` records; `resolve()` constructs. A registered class is **never**
modified — no `__new__`/`__init__` patching, no attributes written onto user
instances. This is the most important thing to understand before touching
`container.py`.

`Container.resolve(cls)` builds a bean in this order:

1. look the `BeanDefinition` up, else `DependencyResolutionError`
2. return `definition.instance` if it is set — either already built, or a
   cycle closing through a field
3. push `cls` onto `Container._resolving`
4. `cls.__new__(cls)` — allocate without running `__init__`
5. `definition.instance = instance` — **early registration**, which is what
   lets a field cycle close instead of recursing forever
6. compute and cache `definition.plan` if absent
7. inject every planned field with `setattr`
8. resolve the planned constructor arguments, refusing any dependency
   already on `_resolving`
9. `cls.__init__(instance, **kwargs)`
10. pop the stack; on any exception, roll back

**Cycle policy.** A cycle is legal only through *fields*. Step 7 has no cycle
check, so a field cycle closes at step 2 with a partially-built partner —
legitimate, because a field is a stored reference read later. Step 8 does have
the check, so a cycle through constructor parameters raises
`CircularDependencyError` with the chain (`A -> B -> A`). A constructor
argument is by contract usable immediately, and handing over a half-built
object would lie about that. An intentional cycle is expressed by converting
the dependency from a constructor parameter to a field.

**Rollback.** A failure clears `instance` on every definition created during
the failed *outermost* `resolve()` call, tracked via `Container._created`, not
just on the bean that failed: a partner already initialized during the same
call may hold a reference to a half-built object. A failed resolution leaves
the container exactly as it was.

**Lazy planning.** `definition.plan` is computed on first resolution, not at
registration. Registration therefore cannot fail because of an annotation
unrelated to injection, and `Autowired["X"]` where `X` is defined later in the
module resolves correctly.

**Thread safety.** A single `threading.RLock` guards the whole of `resolve()`
— reentrant because resolution recurses. Held coarsely, it also keeps
`_resolving` and `_created` single-threaded; per-type locks would risk
deadlock precisely on cycles.

**Consequences to keep in mind.** `Cls()` written by hand is plain Python and
is *not* wired — its `Autowired` fields are absent. Subclasses of a registered
component are ordinary classes. `__slots__` components work as long as they
declare no injected field. Frozen dataclasses and classes whose `__new__`
requires arguments cannot be built by the container and fail with an explicit
`DependencyResolutionError`.
```

In the "Module layout" table, add the `plans.py` row and change the
`definitions.py` row (no more `Scope`):

```markdown
| `definitions.py` | `BeanDefinition`: registration metadata, singleton slot, cached `InjectionPlan` |
| `plans.py` | `InjectionPlan.for_class()`: pure inspection of a class's Autowired fields and constructor parameters |
```

Update the `container.py` row to drop the instrumentation mention, and the
`exceptions.py` row to name the hierarchy.

- [ ] **Step 3: Verify the docs match reality**

Run: `uv run pytest`
Expected: PASS. Then re-read both files against `src/pywire/container.py` and confirm
no sentence still describes monkey-patching, `Scope`, or `PROTOTYPE`.

- [ ] **Step 4: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "📝 Document resolve-time wiring and drop stale claims"
```

---

## Done criteria

- `uv run pytest`, `uv run ruff check .`, and `uv run pyright` all pass.
- `grep -rn "_instrument\|_di_initializing\|_di_initialized\|Scope" src/` returns nothing.
- No sentence in `README.md` or `CLAUDE.md` describes the old mechanism.
- The version is still `0.3.1`; bumping to `0.4.0` awaits the user's explicit request.

## Follow-up (not this plan)

Spec 2: `register_instance` / `register_factory` and `as_type=` supertype binding —
the "push" primitive and Dependency Inversion support. Two error messages written in
Task 5 already point at `register_instance`; their wording is final and needs no
change when spec 2 lands.
