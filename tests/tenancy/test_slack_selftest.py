"""T7-S00 is planned (not required) and its self-test passes against the fake."""

import importlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tenancy_harness.phase5 import PLANNED as PHASE5
from tenancy_harness.phase7 import PLANNED, slack_selftest

PAIR = json.loads(
    (Path(__file__).resolve().parents[2] / "tenancy_harness/pair.json").read_text()
)


def test_t7_s00_is_declared_planned_and_never_required() -> None:
    assert PAIR["planned_controls"]["T7-S00"] == "S0"
    assert "T7-S00" not in PAIR["required_controls"]
    assert not set(PLANNED) & set(PHASE5)
    loaded = importlib.import_module("tenancy_harness.phase7.slack_selftest")
    assert (loaded.CONTROL_ID, loaded.SLICE) == ("T7-S00", "S0")


def test_selftest_passes_against_the_fake(slack: TestClient) -> None:
    slack_selftest.run(slack, "http://testserver")


def test_selftest_fails_when_the_stub_accepts_forgeries(
    slack: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import tenancy_harness.fake_slack as fake

    monkeypatch.setattr(fake, "verify_slack_request", lambda *args, **kwargs: None)
    with pytest.raises(AssertionError, match="forged signature was accepted"):
        slack_selftest.run(slack, "http://testserver")
