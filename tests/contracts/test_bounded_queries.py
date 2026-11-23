"""Phase 5.4: paging primitives, compatible list responses, approvals, overview."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from application.pagination import (
    batches,
    decode_key_cursor,
    decode_time_cursor,
    drain,
    encode_key_cursor,
    encode_time_cursor,
    page_request,
    recent_page_request,
    slice_page,
)
from apps.api.app import create_app
from contracts.v1 import (
    ApprovalRequest,
    ApprovalStatus,
    InvocationStatus,
    RiskLevel,
    TaskAudience,
    ToolInvocation,
)
from tests.support.runtime import LocalPlatformRuntime

API = "/api/v0"


def test_cursors_round_trip_and_reject_garbage():
    assert decode_key_cursor(encode_key_cursor("role-7")) == "role-7"
    stamp = datetime(2026, 11, 24, 10, 30, tzinfo=UTC)
    assert decode_time_cursor(encode_time_cursor(stamp, "a1")) == (stamp, "a1")
    for bad in ("", "###", "e30", encode_time_cursor(stamp, "x")[:-3]):
        with pytest.raises(ValueError):
            decode_key_cursor(bad)
    with pytest.raises(ValueError):
        decode_time_cursor(encode_key_cursor("not-a-time-cursor"))


def test_page_request_bounds_and_envelope_selection():
    assert page_request(None, None).envelope is False
    assert page_request(None, None).effective_limit is None
    assert page_request(None, None, True).envelope is True
    assert page_request(None, None, True).effective_limit == 50
    assert page_request(5, encode_key_cursor("k")).after == "k"
    for bad in (0, 101, -1):
        with pytest.raises(ValueError):
            page_request(bad, None)
    assert recent_page_request(None, None, legacy_default=50).limit == 50
    assert recent_page_request(None, None, paginate=True).limit is None


def test_slice_page_walk_visits_every_item_once_in_key_order():
    items = [f"id-{n:03d}" for n in (5, 1, 9, 3, 7, 2, 8, 4, 6, 0)]
    seen: list[str] = []
    cursor: str | None = None
    while True:
        request = page_request(3, cursor, True)
        page, cursor = slice_page(items, lambda value: value, request)
        assert len(page) <= 3
        seen.extend(page)
        if cursor is None:
            break
    assert seen == sorted(items)


def test_drain_stops_on_short_page_no_progress_and_batch_cap():
    queue = list(range(10))

    def fetch() -> list[int]:
        return queue[:4]

    def handle(item: int) -> bool:
        queue.remove(item)
        return True

    drain(fetch, handle, batch_size=4)
    assert queue == []

    stuck = [1, 2, 3, 4]
    calls: list[int] = []
    drain(lambda: stuck, lambda item: calls.append(item) or False, batch_size=4)
    assert calls == [1, 2, 3, 4]  # one pass, no spin on unhandleable rows

    many = list(range(100))
    walked = list(
        batches(
            lambda after: [n for n in many if after is None or n > after[0]][:5],
            lambda row: (row,),
            batch_size=5,
            max_batches=3,
        )
    )
    assert walked == list(range(15))


def _client():
    runtime = LocalPlatformRuntime(postgres_store=None)
    runtime.bootstrap()
    return runtime, TestClient(create_app(runtime))


def _walk(client: TestClient, path: str, limit: int) -> list[dict]:
    joiner = "&" if "?" in path else "?"
    items: list[dict] = []
    cursor = ""
    for _ in range(50):
        suffix = f"&cursor={cursor}" if cursor else ""
        body = client.get(f"{API}{path}{joiner}paginate=true&limit={limit}{suffix}")
        assert body.status_code == 200, body.text
        payload = body.json()
        assert set(payload) == {"items", "next_cursor"}
        assert len(payload["items"]) <= limit
        items.extend(payload["items"])
        cursor = payload["next_cursor"] or ""
        if not cursor:
            return items
    raise AssertionError("cursor walk did not terminate")


def test_catalog_lists_keep_bare_arrays_and_page_with_keyset_cursors():
    _, client = _client()
    with client:
        for index in range(5):
            created = client.post(
                f"{API}/roles",
                json={"name": f"role-{index}", "permissions": ["tasks.view"]},
            )
            assert created.status_code == 201, created.text
        bare = client.get(f"{API}/roles")
        assert isinstance(bare.json(), list)
        truncated = client.get(f"{API}/roles?limit=2")
        assert isinstance(truncated.json(), list) and len(truncated.json()) == 2
        every_id = {role["id"] for role in bare.json()}
        paged = _walk(client, "/roles", 2)
        assert [role["id"] for role in paged] == sorted(every_id)
        assert len({role["id"] for role in paged}) == len(paged) == len(every_id)
        for path in (
            "/agents",
            "/workflows",
            "/users",
            "/workspaces",
            "/model-profiles",
        ):
            assert isinstance(client.get(f"{API}{path}").json(), list)
            page = client.get(f"{API}{path}?paginate=true&limit=1").json()
            assert set(page) == {"items", "next_cursor"}


def test_paging_parameters_are_validated_not_trusted():
    _, client = _client()
    with client:
        assert client.get(f"{API}/roles?limit=0").status_code == 422
        assert client.get(f"{API}/roles?limit=101&paginate=true").status_code == 422
        assert client.get(f"{API}/roles?cursor=garbage").status_code == 422
        assert client.get(f"{API}/approvals?cursor=garbage").status_code == 422


def _approval(runtime, index: int, status: ApprovalStatus) -> ApprovalRequest:
    task = runtime.task_service.list_tasks()
    org, workspace = task[0].org_id, task[0].workspace_id
    stamp = datetime(2026, 11, 1, tzinfo=UTC) + timedelta(minutes=index)
    resolved = status is not ApprovalStatus.PENDING
    return ApprovalRequest(
        approval_request_id=f"approval-{index:03d}",
        run_id=f"run-{index}",
        requested_by_principal_id="local-developer",
        capability_id="slack.messages.post",
        action_summary=f"approval {index}",
        risk_level=RiskLevel.HIGH,
        status=status,
        requested_at=stamp,
        resolved_by_principal_id="approver" if resolved else None,
        resolved_at=stamp if resolved else None,
        org_id=org,
        workspace_id=workspace,
        created_at=stamp,
        updated_at=stamp,
    )


def test_approvals_page_newest_first_filter_and_batch_their_invocations():
    runtime, client = _client()
    runtime.task_service.create_task(
        agent_id="local-assistant", title="Anchor", initial_message="hello"
    )
    created = [
        _approval(
            runtime,
            index,
            ApprovalStatus.PENDING if index % 3 == 0 else ApprovalStatus.APPROVED,
        )
        for index in range(9)
    ]
    for approval in created:
        runtime.approvals.create(approval)
    stamp = datetime(2026, 11, 2, tzinfo=UTC)
    for approval in created[:4]:
        runtime.tool_invocations.create(
            ToolInvocation(
                invocation_id=f"inv-{approval.approval_request_id}",
                run_id=approval.run_id,
                plugin_installation_id="install",
                capability_id="slack.messages.post",
                status=InvocationStatus.AWAITING_APPROVAL,
                risk_level=RiskLevel.HIGH,
                approval_request_id=approval.approval_request_id,
                org_id=approval.org_id,
                workspace_id=approval.workspace_id,
                created_at=stamp,
                updated_at=stamp,
            )
        )
    with client:
        legacy = client.get(f"{API}/approvals?limit=100")
        assert isinstance(legacy.json(), list) and len(legacy.json()) == 9
        paged = _walk(client, "/approvals", 4)
        ids = [row["approval_request_id"] for row in paged]
        assert ids == sorted(ids, reverse=True) and len(ids) == 9
        with_invocations = {
            row["approval_request_id"]: len(row["tool_invocations"]) for row in paged
        }
        assert [with_invocations[f"approval-{n:03d}"] for n in range(5)] == [
            1,
            1,
            1,
            1,
            0,
        ]
        pending = _walk(client, "/approvals?status=pending", 2)
        assert {row["approval_request_id"] for row in pending} == {
            "approval-000",
            "approval-003",
            "approval-006",
        }


def test_overview_counts_only_what_the_caller_may_see():
    runtime, client = _client()
    runtime.task_service.create_task(
        agent_id="local-assistant", title="Shared", initial_message="one"
    )
    runtime.task_service.create_task(
        agent_id="local-assistant",
        title="Private",
        initial_message="two",
        audience=TaskAudience.PRIVATE,
    )
    runtime.task_worker_service.process_next_turn("overview-worker")
    with client:
        body = client.get(f"{API}/overview").json()
        assert body["tasks"]["total"] == 2
        assert body["turns"]["queued"] + body["turns"]["running"] == 1
        assert sum(day["total"] for day in body["daily"]) == 2
        assert body["pending_approvals"] == 0
        assert body["setup"]["has_agent"] is True
        assert body["generated_at"].endswith("Z")
        assert client.get(f"{API}/overview?tz_offset_minutes=9999").status_code == 422
    other = runtime._repository_set.overview_data.snapshot(
        body_org(runtime),
        body_workspace(runtime),
        "someone-else",
        now=datetime.now(UTC),
        tz_offset_minutes=0,
        include_approvals=False,
    )
    assert other["tasks"]["total"] == 1  # the private task is not theirs
    assert other["pending_approvals"] is None


def body_org(runtime) -> str:
    return runtime.task_service.list_tasks()[0].org_id


def body_workspace(runtime) -> str:
    return runtime.task_service.list_tasks()[0].workspace_id


def test_overview_is_cached_for_a_few_seconds_per_caller():
    runtime, client = _client()
    runtime.task_service.create_task(
        agent_id="local-assistant", title="First", initial_message="one"
    )
    with client:
        first = client.get(f"{API}/overview").json()
        runtime.task_service.create_task(
            agent_id="local-assistant", title="Second", initial_message="two"
        )
        assert client.get(f"{API}/overview").json() == first
        assert (
            client.get(f"{API}/overview?tz_offset_minutes=60").json()["tasks"]["total"]
            == 2
        )
