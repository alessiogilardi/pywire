"""Tests for the Autowired marker's typing contract.

These tests exercise Autowired through the same typing machinery a static
type checker (or any generic-alias-aware tool) relies on, to guarantee that
Autowired[T] exposes T directly instead of an opaque marker type.
"""

from __future__ import annotations

from typing import get_args, get_origin, get_type_hints

import pytest

from pywire import AnnotationResolutionError, Autowired
from pywire.markers import evaluate_annotation, resolve_autowired_type


class Target:
    """Plain class used as the wrapped type in the tests below."""


class _Resolvable:
    dependency: Autowired[Target]


def test_autowired_is_annotated_with_wrapped_type_first() -> None:
    """Autowired[T] must resolve to a generic alias of Autowired itself,
    exposing T as its sole argument, so typing-aware tools see T directly."""
    annotated = Autowired[Target]

    assert get_origin(annotated) is Autowired
    assert get_args(annotated)[0] is Target


def test_autowired_field_type_hint_exposes_wrapped_type() -> None:
    """A field annotated with Autowired[T] must expose T as the sole
    argument when inspected via typing.get_type_hints."""

    class Component:
        dependency: Autowired[Target]

    hints = get_type_hints(Component, include_extras=True)

    assert get_origin(hints["dependency"]) is Autowired
    assert get_args(hints["dependency"])[0] is Target


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
