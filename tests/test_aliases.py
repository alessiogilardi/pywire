"""Tests for the semantic aliases of `component` exposed by `pywire.aliases`."""

import pytest

from pywire import agent, client, component, provider, repository, service


@pytest.mark.parametrize("alias", [service, repository, agent, client, provider])
def test_alias_is_component(alias):
    """Every alias is `component` itself, not a wrapper around it."""
    assert alias is component
