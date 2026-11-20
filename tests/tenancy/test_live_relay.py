"""Prove that budget denial and unpriced requests never reach the provider."""

import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tenancy_harness.budget import Ledger, initialize
from tenancy_harness.live_relay import relay
from tests.tenancy.test_budget import config


@pytest.fixture
def relay_client(tmp_path, monkeypatch):
    initialize(tmp_path, config(1_000_000_000))
    credentials = tmp_path / "live-config.json"
    credentials.write_text(
        json.dumps({"api_key": "fixture-only", "run_id": "test-run"})
    )
    monkeypatch.setenv("TENANCY_LIVE_CONFIG", str(credentials))
    monkeypatch.setenv("TENANCY_BUDGET_DIR", str(tmp_path))
    app = FastAPI()
    app.add_api_route("/v1/{endpoint}", relay, methods=["GET", "POST"])
    with TestClient(app) as client:
        yield client, Ledger(tmp_path)


def forbid_upstream(*args, **kwargs):
    raise AssertionError("Denied request attempted an actual provider call")


def test_exhausted_budget_returns_402_without_network_dispatch(
    relay_client, monkeypatch
):
    client, ledger = relay_client
    ledger.reserve("model", "earlier-run", 3)
    monkeypatch.setattr(httpx, "AsyncClient", forbid_upstream)
    response = client.post(
        "/v1/messages",
        headers={"x-api-key": "fixture-only"},
        json={"model": "model", "messages": [], "max_tokens": 128},
    )
    assert response.status_code == 402
    assert ledger.snapshot()["requests"] == 1
    assert ledger.snapshot()["available_usd"] == "0"


def test_unpriced_features_are_rejected_before_reservation(relay_client, monkeypatch):
    client, ledger = relay_client
    monkeypatch.setattr(httpx, "AsyncClient", forbid_upstream)
    response = client.post(
        "/v1/messages",
        headers={"x-api-key": "fixture-only"},
        json={"model": "model", "max_tokens": 128, "tools": [{}]},
    )
    assert response.status_code == 400
    assert ledger.snapshot()["requests"] == 0


def test_wrong_key_is_rejected_without_provider_or_budget_use(
    relay_client, monkeypatch
):
    client, ledger = relay_client
    monkeypatch.setattr(httpx, "AsyncClient", forbid_upstream)
    response = client.get("/v1/models", headers={"x-api-key": "wrong"})
    assert response.status_code == 401
    assert ledger.snapshot()["requests"] == 0


def test_larger_thinking_envelope_still_blocks_over_limit_before_dispatch(
    relay_client, monkeypatch
):
    client, ledger = relay_client
    monkeypatch.setattr(httpx, "AsyncClient", forbid_upstream)
    response = client.post(
        "/v1/messages",
        headers={"x-api-key": "fixture-only"},
        json={"model": "model", "max_tokens": 1025, "messages": []},
    )
    assert response.status_code == 400
    assert ledger.snapshot()["requests"] == 0
