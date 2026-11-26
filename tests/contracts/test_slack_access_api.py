"""The Slack access settings API: permission, versions, allowlists, audit."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from apps.api.app import create_app
from apps.runtime_wiring import compose_platform_runtime
from contracts.v1 import Permission
from modules.slack.access.decision_audit import DecisionContext, DecisionRecorder
from modules.slack.access.engine import evaluate
from modules.slack.access.models import Action
from tests.support.organization_administration import headers, scope
from tests.support.slack_access_cases import request, settings
from tests.support.slack_access_world import (
    ASKER,
    BASE,
    CHANNEL,
    OTHER,
    build_world,
    settings_body,
)


@pytest.fixture
def world():
    runtime = compose_platform_runtime(
        http_client=httpx.Client(), store=None, seed_local_defaults=False
    )
    runtime.bootstrap()
    world = build_world(runtime)
    with TestClient(create_app(runtime), raise_server_exceptions=False) as client:
        world["client"] = client
        yield world
    runtime.close()


def admin(world):
    return headers(world)


def member(world):
    return headers(world, "member_token")


def events(world, tenant="a", prefix="slack."):
    with scope(world[tenant]):
        rows = world["runtime"].audit_events.list_scoped(world[tenant][0].id)
    return [e for e in rows if e.action.startswith(prefix)]


def test_slack_manage_is_a_permission_and_admin_has_it(world):
    assert Permission.SLACK_MANAGE.value == "slack.manage"
    with scope(world["a"]):
        roles = world["runtime"].roles.list_scoped(world["a"][0].id)
    by_key = {r.system_role_key: r for r in roles}
    assert Permission.SLACK_MANAGE in by_key["admin"].permissions
    assert Permission.SLACK_MANAGE not in by_key["builder"].permissions
    assert Permission.SLACK_MANAGE not in by_key["user"].permissions


def test_defaults_before_any_change(world):
    response = world["client"].get(BASE, headers=admin(world))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["settings_version"] == 0 and body["paused"] is False
    assert body["ask_enabled"] is True and body["partial_answers"] is False
    assert body["connector_audience"] == {} and body["planner_profile_id"] is None
    assert body["rules"] == {
        "create_task": "off",
        "pr_review": "off",
        "run_workflow": "off",
        "ask_private": "off",
    }
    assert body["ask_limits"] == {
        "user_per_10_min": 5,
        "channel_per_10_min": 20,
        "org_per_day": 200,
        "max_question_chars": 1000,
    }
    assert body["allowed_people_count"] == 0 and body["allowed_channel_count"] == 0
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", BASE, None),
        ("PUT", BASE, "settings"),
        ("POST", BASE + "/pause", {"paused": True}),
        ("GET", BASE + "/people", None),
        ("POST", BASE + "/people", {"slack_user_id": ASKER}),
        ("DELETE", BASE + f"/people/{ASKER}", None),
        ("GET", BASE + "/channels", None),
        ("POST", BASE + "/channels", {"channel_id": CHANNEL}),
        ("DELETE", BASE + f"/channels/{CHANNEL}", None),
        ("GET", BASE + "/attempts", None),
    ],
)
def test_only_slack_managers_may_use_any_route(world, method, path, body):
    client = world["client"]
    body = settings_body(0) if body == "settings" else body
    refused = client.request(method, path, headers=member(world), json=body)
    assert refused.status_code == 403, (path, refused.text)
    assert client.request(method, path, json=body).status_code in (401, 403)
    assert not events(world)  # nothing was changed or audited


def test_settings_replace_with_optimistic_version_and_audit(world):
    client = world["client"]
    body = settings_body(
        0,
        partial_answers=True,
        connector_audience={"inst-jira": "allowed_people"},
        rules={
            "create_task": "user_or_channel",
            "pr_review": "user_and_channel",
            "run_workflow": "off",
            "ask_private": "user_only",
        },
        pr_review_repos=["Acme/API", "acme/api", "acme/web"],
        workflow_ids=["wf-1", "wf-1"],
        ask_limits={
            "user_per_10_min": 2,
            "channel_per_10_min": 9,
            "org_per_day": 50,
            "max_question_chars": 500,
        },
    )
    saved = client.put(BASE, headers=admin(world), json=body)
    assert saved.status_code == 200, saved.text
    out = saved.json()
    assert out["settings_version"] == 1
    assert out["pr_review_repos"] == ["acme/api", "acme/web"]
    assert out["workflow_ids"] == ["wf-1"]
    assert out["updated_by"] == world["a"][3].id
    assert client.get(BASE, headers=admin(world)).json() == out
    stale = client.put(BASE, headers=admin(world), json=body)
    assert stale.status_code == 409
    again = client.put(BASE, headers=admin(world), json={**body, "expected_version": 1})
    assert again.status_code == 200 and again.json()["settings_version"] == 2
    audit = [e for e in events(world) if e.action == "slack.access.settings_updated"]
    assert len(audit) == 2
    first = sorted(audit, key=lambda e: e.created_at)[0].metadata
    assert first["settings_version"] == 1 and first["previous_version"] == 0
    assert (
        "partial_answers" in first["changed"]
        and first["rules"]["pr_review"] == "user_and_channel"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {
            "rules": {
                "create_task": "sometimes",
                "pr_review": "off",
                "run_workflow": "off",
                "ask_private": "off",
            }
        },
        {"connector_audience": {"inst-jira": "world"}},
        {"connector_audience": {"inst-unknown": "everyone"}},
        {"planner_profile_id": "no-such-profile"},
        {"pr_review_repos": ["not a repo"]},
        {
            "ask_limits": {
                "user_per_10_min": 0,
                "channel_per_10_min": 1,
                "org_per_day": 1,
                "max_question_chars": 100,
            }
        },
        {
            "ask_limits": {
                "user_per_10_min": 1,
                "channel_per_10_min": 1,
                "org_per_day": 6000,
                "max_question_chars": 100,
            }
        },
        {"unknown_field": True},
        {"expected_version": -1},
        {"paused": True},  # pause has its own route; not part of the replacement
    ],
)
def test_invalid_settings_are_rejected_without_a_change(world, changes):
    client = world["client"]
    response = client.put(BASE, headers=admin(world), json=settings_body(0, **changes))
    assert response.status_code == 422, response.text
    assert client.get(BASE, headers=admin(world)).json()["settings_version"] == 0
    assert not events(world)


def test_pause_resume_bumps_version_and_survives_a_settings_replace(world):
    client = world["client"]
    paused = client.post(BASE + "/pause", headers=admin(world), json={"paused": True})
    assert paused.status_code == 200 and paused.json()["paused"] is True
    assert paused.json()["settings_version"] == 1
    body = settings_body(1, ask_enabled=False)
    kept = client.put(BASE, headers=admin(world), json=body).json()
    assert kept["paused"] is True and kept["ask_enabled"] is False
    resumed = client.post(BASE + "/pause", headers=admin(world), json={"paused": False})
    assert resumed.json()["paused"] is False and resumed.json()["settings_version"] == 3
    actions = {e.action for e in events(world)}
    assert {"slack.access.paused", "slack.access.resumed"} <= actions
    assert (
        client.post(
            BASE + "/pause", headers=admin(world), json={"paused": "x"}
        ).status_code
        == 422
    )


def test_allowed_people_with_asserted_member_mapping(world):
    client, h = world["client"], admin(world)
    someone = world["member_a"][1].id
    added = client.post(
        BASE + "/people",
        headers=h,
        json={"slack_user_id": ASKER, "principal_id": someone, "note": " on-call "},
    )
    assert added.status_code == 200, added.text
    row = added.json()
    assert row["principal_id"] == someone and row["note"] == "on-call"
    assert row["member_name"] and row["added_by"] == world["a"][3].id
    unmapped = client.post(BASE + "/people", headers=h, json={"slack_user_id": OTHER})
    assert unmapped.json()["principal_id"] is None
    listing = client.get(BASE + "/people", headers=h).json()
    assert [r["slack_user_id"] for r in listing] == [ASKER, OTHER]
    assert client.get(BASE, headers=h).json()["allowed_people_count"] == 2
    # Updating the same Slack id re-asserts the mapping instead of adding a row.
    remap = client.post(BASE + "/people", headers=h, json={"slack_user_id": ASKER})
    assert remap.json()["principal_id"] is None
    assert len(client.get(BASE + "/people", headers=h).json()) == 2
    assert client.delete(BASE + f"/people/{ASKER}", headers=h).status_code == 204
    assert client.delete(BASE + f"/people/{ASKER}", headers=h).status_code == 404
    kinds = [e.action for e in sorted(events(world), key=lambda e: e.created_at)]
    assert kinds.count("slack.access.user_added") == 2
    assert kinds.count("slack.access.user_updated") == 1
    assert kinds.count("slack.access.user_removed") == 1
    assert client.get(BASE, headers=h).json()["settings_version"] == 4
    asserted = next(e for e in events(world) if e.action == "slack.access.user_added")
    assert asserted.actor_principal_id == world["a"][3].id
    assert asserted.metadata["slack_user_id"] == ASKER
    assert asserted.metadata["principal_id"] == someone


@pytest.mark.parametrize(
    "bad", ["u0lower01", "X0ABCDEF", "U", "U0ASKER01 ", "C0CHAN001"]
)
def test_slack_user_ids_are_validated(world, bad):
    response = world["client"].post(
        BASE + "/people", headers=admin(world), json={"slack_user_id": bad}
    )
    assert response.status_code == 422


def test_mapping_must_name_an_active_member_of_this_organization(world):
    client, h = world["client"], admin(world)
    cases = [
        world["member_b"][1].id,  # a member of another organization
        "no-such-principal",
    ]
    for principal in cases:
        refused = client.post(
            BASE + "/people",
            headers=h,
            json={"slack_user_id": ASKER, "principal_id": principal},
        )
        assert refused.status_code == 422, principal
    assert client.get(BASE + "/people", headers=h).json() == []


def test_allowed_channels(world):
    client, h = world["client"], admin(world)
    added = client.post(
        BASE + "/channels",
        headers=h,
        json={"channel_id": CHANNEL, "name_cached": "eng"},
    )
    assert added.status_code == 200 and added.json()["name"] == "eng"
    private = client.post(
        BASE + "/channels", headers=h, json={"channel_id": "G0PRIVATE"}
    )
    assert private.status_code == 200
    for bad in ("D0DIRECT1", "c0lower01", "C"):  # DMs can never be allowlisted
        assert (
            client.post(
                BASE + "/channels", headers=h, json={"channel_id": bad}
            ).status_code
            == 422
        )
    listing = client.get(BASE + "/channels", headers=h).json()
    assert [r["channel_id"] for r in listing] == [CHANNEL, "G0PRIVATE"]
    assert client.delete(BASE + f"/channels/{CHANNEL}", headers=h).status_code == 204
    assert client.delete(BASE + f"/channels/{CHANNEL}", headers=h).status_code == 404
    assert client.delete(BASE + "/channels/D0DIRECT1", headers=h).status_code == 422
    assert client.get(BASE, headers=h).json()["allowed_channel_count"] == 1
    kinds = [e.action for e in events(world)]
    assert kinds.count("slack.access.channel_added") == 2
    assert kinds.count("slack.access.channel_removed") == 1


def test_organizations_never_see_each_others_slack_access(world):
    client = world["client"]
    client.post(BASE + "/people", headers=admin(world), json={"slack_user_id": ASKER})
    client.put(BASE, headers=admin(world), json=settings_body(1, ask_enabled=False))
    other = headers(world, "b_token", "b")
    assert client.get(BASE, headers=other).json()["settings_version"] == 0
    assert client.get(BASE + "/people", headers=other).json() == []
    assert client.delete(BASE + f"/people/{ASKER}", headers=other).status_code == 404
    assert events(world, "b") == []


def record_attempts(world, count: int):
    runtime = world["runtime"]
    org = world["a"][0].id
    recorder = DecisionRecorder(runtime.audit_events)
    base = datetime.now(UTC) - timedelta(hours=2)
    with scope(world["a"]):
        for i in range(count):
            allowed = i % 2 == 0
            decision = evaluate(
                request(
                    Action.CREATE_TASK,
                    settings=settings(rule="user_only" if allowed else "off"),
                    user_listed=True,
                )
            )
            ctx = DecisionContext(
                org_id=org,
                event_id=f"Ev{i}",
                request_id=f"slack-Ev{i}",
                slack_user_id=f"U0USER{i:03d}",
                channel_id=CHANNEL,
            )
            recorder.record(decision, ctx, base + timedelta(seconds=i))


def test_recent_attempts_are_the_last_fifty_without_any_text(world):
    record_attempts(world, 70)
    response = world["client"].get(BASE + "/attempts", headers=admin(world))
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 50
    assert [r["at"] for r in rows] == sorted((r["at"] for r in rows), reverse=True)
    assert {r["allowed"] for r in rows} == {True, False}
    assert set(rows[0]) == {
        "at",
        "allowed",
        "action",
        "outcome",
        "reason",
        "slack_user_id",
        "channel_id",
        "principal_id",
        "repeat_count",
        "settings_version",
    }
    assert rows[0]["action"] == "create_task"
    assert "text" not in response.text.lower().replace("context", "")


def test_runtime_standing_exposes_only_the_askers_own_flags(world):
    client, h = world["client"], admin(world)
    someone = world["member_a"][1].id
    client.post(
        BASE + "/people",
        headers=h,
        json={"slack_user_id": ASKER, "principal_id": someone},
    )
    client.post(BASE + "/people", headers=h, json={"slack_user_id": OTHER})
    client.post(BASE + "/channels", headers=h, json={"channel_id": CHANNEL})
    service = world["runtime"].slack_access_service
    with scope(world["a"]):
        listed, channel, standing = service.asker_standing(ASKER, CHANNEL)
        assert (listed, channel) == (True, True)
        assert standing is not None and standing.principal_id == someone
        assert standing.active and "tasks.create" in standing.permissions
        assert "slack.manage" not in standing.permissions
        listed, channel, standing = service.asker_standing(OTHER, "C0ELSE001")
        assert (listed, channel, standing) == (True, False, None)
        assert service.asker_standing("U0NOBODY1", CHANNEL)[:1] == (False,)
        assert service.policy_settings().version == 3
