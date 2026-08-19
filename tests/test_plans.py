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
