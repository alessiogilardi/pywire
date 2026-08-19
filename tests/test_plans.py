"""Tests for InjectionPlan, the description of what a class needs.

These run without a Container: planning is pure inspection.
"""

from __future__ import annotations

import dataclasses

import pytest

from pywire import AnnotationResolutionError, Autowired, UnconstructibleComponentError
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
    dep: Autowired["LateDefined"]  # noqa: UP037


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
    broken: NeverDefinedAnywhere  # noqa: F821  # pyright: ignore[reportUndefinedVariable]


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
    dep: Autowired["NotAThing"]  # noqa: F821, UP037  # pyright: ignore[reportUndefinedVariable]


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
