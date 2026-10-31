import json
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from apps.api.app import create_app
from contracts.v1 import AuditEvent, AuditTargetScope
from tests.support.runtime import LocalPlatformRuntime


def _audit_event(event_id: str, created_at: datetime) -> AuditEvent:
    return AuditEvent(
        audit_event_id=event_id,
        org_id="local-org",
        target_scope=AuditTargetScope.WORKSPACE,
        workspace_id="local-workspace",
        actor_principal_id="local-developer",
        action="test.export",
        target_type="export_test",
        target_id=event_id,
        summary=f"Test event {event_id}",
        request_id=event_id,
        metadata={"sequence": event_id},
        created_at=created_at,
    )


def test_audit_export_paginates_stably_and_preserves_filters():
    runtime = LocalPlatformRuntime()
    runtime.bootstrap()
    timestamp = datetime(2026, 10, 6, tzinfo=UTC)
    for event_id in ("event-a", "event-b", "event-c", "event-d"):
        runtime.audit_events.create(_audit_event(event_id, timestamp))
    client = TestClient(create_app(runtime))

    with client:
        first = client.get(
            "/api/v0/audit-events/export",
            params={"limit": 2, "action": "test.export"},
        )
        assert first.status_code == 200, first.text
        assert first.headers["content-type"].startswith("application/x-ndjson")
        assert first.headers["cache-control"] == "no-store"
        first_events = [json.loads(line) for line in first.text.splitlines()]
        assert len(first_events) == 2
        assert all(event["action"] == "test.export" for event in first_events)
        cursor = first.headers.get("x-next-cursor")
        assert cursor

        second = client.get(
            "/api/v0/audit-events/export",
            params={"limit": 2, "action": "test.export", "cursor": cursor},
        )
        assert second.status_code == 200, second.text
        second_events = [json.loads(line) for line in second.text.splitlines()]
        assert len(second_events) == 2
        assert "x-next-cursor" not in second.headers

    event_ids = [event["audit_event_id"] for event in first_events + second_events]
    assert len(event_ids) == len(set(event_ids)) == 4


def test_audit_export_requires_a_valid_cursor():
    client = TestClient(create_app(LocalPlatformRuntime()))

    with client:
        response = client.get("/api/v0/audit-events/export", params={"cursor": "bad"})

    assert response.status_code == 422
    assert response.json()["detail"] == "audit cursor is invalid"
