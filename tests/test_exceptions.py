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
        from pywire import BeanDefinition  # noqa: F401 # type: ignore[attr-defined]
