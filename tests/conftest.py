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
