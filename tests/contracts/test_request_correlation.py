"""A request id flows from the HTTP response into the job, worker logs and audit."""

from __future__ import annotations

import io
import json
import logging

import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app
from shared.logging_setup import JsonFormatter
from tests.support.runtime import LocalPlatformRuntime
from tests.support.task_execution import run_task

RID = "browser-req-7f3a9c21"


@pytest.fixture
def json_logs():
    """Capture every logger's output through the production JSON formatter."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter("test"))
    root = logging.getLogger()
    level = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    yield lambda: [json.loads(line) for line in stream.getvalue().splitlines()]
    root.removeHandler(handler)
    root.setLevel(level)


def test_inbound_request_id_is_kept_only_when_well_formed() -> None:
    client = TestClient(create_app(LocalPlatformRuntime()))
    with client:
        kept = client.get("/api/v0/health", headers={"X-Request-Id": RID})
        replaced = client.get("/api/v0/health", headers={"X-Request-Id": "a b<c>"})
        fresh = client.get("/api/v0/health")
    assert kept.headers["X-Request-Id"] == RID
    assert replaced.headers["X-Request-Id"] != "a b<c>"
    assert len(replaced.headers["X-Request-Id"]) == 36
    assert fresh.headers["X-Request-Id"] != replaced.headers["X-Request-Id"]


def test_request_id_reaches_the_turn_worker_logs_and_audit_trail(json_logs) -> None:
    runtime = LocalPlatformRuntime()
    client = TestClient(create_app(runtime))
    with client:
        task, turn, _agent_run, _ = run_task(
            client, runtime, "TOP-SECRET-PROMPT-SENTINEL", headers={"X-Request-Id": RID}
        )
    assert turn.request_id == RID
    lines = json_logs()
    api = [
        x for x in lines if x.get("event") == "http_request" and x["request_id"] == RID
    ]
    assert api and api[0]["route"] == "/api/v0/tasks" and api[0]["status"] == 202
    assert api[0]["org_id"] == task["org_id"] and api[0]["workspace_id"]
    worker = [x for x in lines if x.get("event") == "turn_finished"]
    assert worker[0]["request_id"] == RID and worker[0]["turn_id"] == turn.turn_id
    assert worker[0]["run_id"] == turn.agent_run_id
    assert "TOP-SECRET-PROMPT-SENTINEL" not in json.dumps(lines)
    audit = [
        event
        for event in runtime.audit_events.list_scoped(
            task["org_id"], task["workspace_id"]
        )
        if event.correlation_id == RID
    ]
    assert audit, "run audit events carry the originating request id"


def test_request_without_inbound_id_uses_the_response_id_everywhere(json_logs) -> None:
    runtime = LocalPlatformRuntime()
    client = TestClient(create_app(runtime))
    with client:
        response = client.post(
            "/api/v0/tasks",
            json={
                "agent_id": "local-assistant",
                "title": "t",
                "initial_message": "hello there",
            },
        )
        turn = runtime.task_worker_service.process_next_turn("w1")
    rid = response.headers["X-Request-Id"]
    assert turn is not None and turn.request_id == rid
    assert any(x.get("request_id") == rid and x.get("turn_id") for x in json_logs())


def test_internal_metrics_are_served_directly_and_hidden_behind_a_proxy(
    monkeypatch,
) -> None:
    monkeypatch.delenv("WEAVES_METRICS_TOKEN", raising=False)
    client = TestClient(create_app(LocalPlatformRuntime()))
    with client:
        client.get("/api/v0/health")
        direct = client.get("/api/v0/internal/metrics")
        proxied = client.get(
            "/api/v0/internal/metrics", headers={"X-Forwarded-For": "203.0.113.9"}
        )
        gateway = client.get(
            "/api/v0/internal/metrics", headers={"X-Forwarded-Proto": "https"}
        )
        monkeypatch.setenv("WEAVES_METRICS_TOKEN", "metrics-token-123456")
        no_token = client.get("/api/v0/internal/metrics")
        with_token = client.get(
            "/api/v0/internal/metrics",
            headers={"Authorization": "Bearer metrics-token-123456"},
        )
    assert direct.status_code == 200 and "text/plain" in direct.headers["content-type"]
    assert (
        'http_request_seconds_count{route_template="/api/v0/health",'
        'status_class="2xx"}' in direct.text
    )
    assert "org_id" not in direct.text and "local-org" not in direct.text
    assert proxied.status_code == 404 and gateway.status_code == 404
    assert no_token.status_code == 404 and with_token.status_code == 200


def test_unmatched_paths_share_one_route_template() -> None:
    client = TestClient(create_app(LocalPlatformRuntime()))
    with client:
        for index in range(5):
            client.get(f"/not-a-route/{index}")
        text = client.get("/api/v0/internal/metrics").text
    assert "/not-a-route" not in text
    assert 'route_template="unmatched"' in text
