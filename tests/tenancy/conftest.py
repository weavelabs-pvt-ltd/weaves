"""Fixtures for the fake Slack server tests (injected clock, no real waiting)."""

from collections.abc import Iterator

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tenancy_harness.fake_slack import router
from tenancy_harness.fake_slack_state import DEFAULT_BOT_TOKEN, STATE

START = 1_700_000_000.0


class FakeClock:
    """A settable clock; ``sleep`` records the request and advances time."""

    def __init__(self) -> None:
        self.now = START
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock() -> Iterator[FakeClock]:
    fake = FakeClock()
    saved = (STATE.clock, STATE.sleep, STATE.transport)
    STATE.reset()
    STATE.clock, STATE.sleep = fake, fake.sleep
    yield fake
    STATE.clock, STATE.sleep, STATE.transport = saved
    STATE.reset()


@pytest.fixture
def slack_app(clock: FakeClock) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    STATE.transport = httpx.ASGITransport(app=app)
    return app


@pytest.fixture
def slack(slack_app: FastAPI) -> Iterator[TestClient]:
    with TestClient(slack_app, base_url="http://testserver") as client:
        yield client


@pytest.fixture
def bot_token() -> str:
    return DEFAULT_BOT_TOKEN
