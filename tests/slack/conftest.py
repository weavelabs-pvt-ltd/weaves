"""Fixtures for the Slack install, events and pipeline tests."""

from collections.abc import Iterator

import pytest

from tests.slack.support import World, running_world
from tests.support import slack_pipeline_world as pipeline
from tests.support.slack_ask_world import AskWorld, build_ask_world
from tests.support.slack_pipeline_world import World as PipelineWorld


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> Iterator[World]:
    """A Slack-enabled API with two organizations (alpha, beta)."""
    with running_world(monkeypatch) as running:
        yield running


@pytest.fixture
def pworld(monkeypatch: pytest.MonkeyPatch) -> Iterator[PipelineWorld]:
    """A Slack-ready local runtime with counting fake connectors and a fake model."""
    monkeypatch.setenv("WEAVES_SLACK_ENABLED", "true")
    monkeypatch.setenv("WEAVES_SLACK_SIGNING_SECRET", "fixture-signing-secret")
    built = pipeline.build_world()
    try:
        yield built
    finally:
        built.close()


@pytest.fixture
def aworld(monkeypatch: pytest.MonkeyPatch) -> Iterator[AskWorld]:
    """A Slack Ask world: the real ticks over a recording fake Slack Web API."""
    monkeypatch.setenv("WEAVES_SLACK_ENABLED", "true")
    monkeypatch.setenv("WEAVES_SLACK_SIGNING_SECRET", "fixture-signing-secret")
    built = build_ask_world()
    try:
        yield built
    finally:
        built.world.close()
