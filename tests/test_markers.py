"""Tests for the Autowired marker's typing contract.

These tests exercise Autowired through the same typing machinery a static
type checker (or any Annotated-aware tool) relies on, to guarantee that
Autowired[T] exposes T directly instead of an opaque marker type.
"""

from __future__ import annotations

from typing import Annotated, get_args, get_origin, get_type_hints

from pywire import Autowired


class Target:
    """Plain class used as the wrapped type in the tests below."""

def test_autowired_is_annotated_with_wrapped_type_first() -> None:
    """Autowired[T] must resolve to Annotated[T, ...] so typing-aware tools
    see T, not a private marker class."""
    annotated = Autowired[Target]

    assert get_origin(annotated) is Annotated
    assert get_args(annotated)[0] is Target


def test_autowired_field_type_hint_exposes_wrapped_type() -> None:
    """A field annotated with Autowired[T] must expose T as the first
    Annotated argument when inspected via typing.get_type_hints."""

    class Component:
        dependency: Autowired[Target]

    hints = get_type_hints(Component, include_extras=True)

    assert get_origin(hints["dependency"]) is Annotated
    assert get_args(hints["dependency"])[0] is Target
