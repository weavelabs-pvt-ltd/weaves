"""Fixtures for the Slack install and events tests."""

from collections.abc import Iterator

import pytest

from tests.slack.support import World, running_world


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    """A Slack-enabled API with two organizations (alpha, beta)."""
    with running_world(monkeypatch) as running:
        yield running
