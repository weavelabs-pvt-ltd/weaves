"""Control T7-I06: ``@Weaves task:`` on the real stack (slice S5).

Slack is the fake server and the model is the fake provider (it echoes its prompt).
Everything else is real: the signed mention goes through the running API into
PostgreSQL, the worker's dispatch tick evaluates the access rule and creates a
NORMAL workspace task as the mapped member, the worker guard and agent run it, and
the reply tick posts to the fake Slack with the bot token from the secret store.

Checked against the fake Slack, the API and the database: the task is created as
the mapped member and listed for the admin with its Slack origin (channel, who asked,
a thread link, and nothing of the Slack ids), the first message "Task created" is
edited into "Task done" and the result is ONE new message with the link (two posts
in all, the egress scrubber applied), a repeat ``task:`` in the thread answers
"Already tracked" with a View task button and creates nothing, a replayed event adds
nothing, an empty ``task:`` asks for the text, a rule that is off, an unset default
agent and the Execute limit are each told privately and create no task, the default
agent is validated by the settings API, thread rows are invisible to other tenants
and no task text reaches the thread rows or the audit trail.
"""

import json
import time

from tenancy_harness.client import call, request
from tenancy_harness.phase7.slack_ask_checks import (
    CHANNEL,
    FIELDS,
    PATH,
    bot_posts,
    human_message,
    inbox_state,
    settled,
    settled_inbox,
    threads,
    whispers,
)
from tenancy_harness.phase7.slack_support import (
    deliver,
    install,
    mention,
    record,
    reset_fake,
    rows,
    scoped,
)
from tenancy_harness.resources import tenant
from tenancy_harness.seed import require_disposable_database

CONTROL_ID = "T7-I06"
SLICE = "S5"
FIXTURE = "slack_task"
USER = "U0HUMAN01"
BOT = "<@U0FAKEBOT> "
MARKER = "ZEBRATASK7731"
LEAK = "xoxb-fixture-leak-1234567890"
EVENTS = iter(f"Ev7T{n:03d}" for n in range(1, 999))
CREATED = "Task created: "
NOT_SET_UP = ":lock: I'm not set up to create tasks here. Ask a Weaves admin."
NO_AGENT = "No agent is set up for Slack tasks. Ask a Weaves admin."
MISSING_TEXT = (
    "Tell me what the task is, for example @Weaves task: prepare the weekly KPI summary"
)


def task_mention(text, thread_ts=None, event_id=None):
    """Deliver ``@Weaves task: text``; returns (event id, message ts, report)."""
    ts = human_message(thread_ts)
    event_id = event_id or next(EVENTS)
    report = deliver(
        mention(event_id, ts, text=BOT + text, thread_ts=thread_ts, user=USER)
    )
    return event_id, ts, report


def tasks(org_id):
    """Tasks created from Slack in the organization (payloads, oldest first)."""
    found = rows(
        "SELECT payload FROM product_records WHERE collection = 'tasks' "
        "AND org_id = %s AND idempotency_key LIKE 'slack:%%' ORDER BY created_at",
        org_id,
    )
    return [row[0] for row in found]


def executes(org_id, action):
    """Audit events of one ``slack.execute.*`` action (allowed or blocked)."""
    return rows(
        "SELECT payload FROM product_records WHERE collection = 'audit_events' "
        "AND org_id = %s AND payload->>'action' = %s",
        org_id,
        "slack.execute." + action,
    )


def put_settings(a, **changes):
    """Replace the Slack access settings through the admin API; returns the status."""
    view = request("GET", PATH, a["admin"]["headers"]).json()
    body = {k: view[k] for k in view if k in FIELDS | {"execute_limits"}}
    body["default_task_agent_id"] = view.get("default_task_agent_id")
    body.update(changes)
    body["expected_version"] = view["settings_version"]
    return request("PUT", PATH, a["admin"]["headers"], body).status_code


def button(message):
    """(label, url) of a message's link button, or None."""
    for block in message.get("blocks") or []:
        for element in block.get("elements", []):
            return element["text"]["text"], element["url"]
    return None


def set_up(a):
    """The agent, the allowlisted member mapped to the admin, and the rule."""
    agent = call(
        "POST",
        "/agents",
        a["admin"],
        {
            "name": "Ops assistant",
            "instructions": "Do the task the user describes.",
            "description": a["marker"],
            "allowed_capability_ids": [],
        },
    )
    mapped = request(
        "POST",
        PATH + "/people",
        a["admin"]["headers"],
        {"slack_user_id": USER, "principal_id": a["admin"]["principal_id"]},
    )
    rules = {
        "create_task": "user_only",
        "pr_review": "off",
        "run_workflow": "off",
        "ask_private": "off",
    }
    saved = put_settings(a, rules=rules, default_task_agent_id=agent["id"])
    return agent, mapped.status_code == 200 and saved == 200


def creation_results(a, agent):
    """A normal task as the mapped member, its messages and its origin."""
    org = a["org_id"]
    text = f"prepare the weekly KPI summary {MARKER} {LEAK} <!channel>"
    event_id, ts, _ = task_mention(f"task: {text}")
    ok = settled(org, event_id, limit=120)
    (made,) = [t for t in tasks(org) if t["idempotency_key"].endswith(event_id)]
    listed = request("GET", "/tasks?origin=slack", a["admin"]["headers"]).json()
    everything = request("GET", "/tasks", a["admin"]["headers"]).json()
    detail = request("GET", f"/tasks/{made['task_id']}", a["admin"]["headers"]).json()
    turns = request(
        "GET", f"/tasks/{made['task_id']}/turns", a["admin"]["headers"]
    ).json()
    posts = bot_posts(ts)
    (row,) = [t for t in threads(org) if t["event_id"] == event_id]
    origin = detail.get("origin") or {}
    blob = json.dumps(detail)
    first, result = [*posts, {}, {}][:2]
    link = button(first)
    return {
        "the_task_is_a_normal_workspace_task_created_as_the_mapped_member": ok
        and made["audience"] == "workspace"
        and made["created_by_principal_id"] == a["admin"]["principal_id"]
        and made["agent_id"] == agent["id"]
        and made["title"].startswith("prepare the weekly KPI summary"),
        "it_is_listed_with_its_slack_origin_and_in_the_normal_list": any(
            i["task_id"] == made["task_id"] for i in listed.get("items", [])
        )
        and any(i["task_id"] == made["task_id"] for i in everything.get("items", []))
        and origin.get("kind") == "slack"
        and origin.get("channel_id") == CHANNEL
        and bool(origin.get("requested_by_name")),
        "the_origin_has_a_slack_thread_link_and_no_slack_internals": origin.get(
            "permalink", ""
        ).startswith("https://fixture-team.slack.com/archives/" + CHANNEL)
        and USER not in blob
        and "T0FAKE001" not in json.dumps(origin)
        and "origin" not in json.dumps(turns),
        "the_first_message_is_created_then_edited_into_the_receipt": len(posts) == 2
        and first.get("text", "").startswith(":white_check_mark: Task done: ")
        and " · Ops assistant · " in first["text"],
        "the_result_is_one_new_message_with_the_full_result_pointer": result.get(
            "text", ""
        ).endswith("Full result and sources in Weaves.")
        and result["ts"] != first["ts"],
        "both_messages_link_to_the_task_in_weaves": link is not None
        and link[1].endswith(f"/#tasks/{made['task_id']}")
        and (button(result) or ("", ""))[0] == "Open result",
        "never_more_than_two_posts_per_request": row["posts"] == 2
        and row["reply_state"] == "posted"
        and row["run_state"] == "done"
        and row["kind"] == "task",
        "the_egress_scrubber_and_sanitizer_hold": LEAK not in json.dumps(posts)
        and "<!channel>" not in json.dumps(posts),
        "the_inbox_row_is_settled_without_its_text": inbox_state(event_id)
        == ("done", None),
        "one_allowed_execute_record_per_request": len(executes(org, "allowed")) == 1,
        "_first": (made["task_id"], ts, event_id, text),
    }


def repeat_results(a, first):
    """A repeat ``task:`` in the thread, a replay and an empty ``task:``."""
    org = a["org_id"]
    task_id, ts, event_id, _ = first
    before = len(tasks(org))
    again, _, _ = task_mention("task: something else entirely", thread_ts=ts)
    settled_inbox(again)
    time.sleep(2)
    tracked = [p for p in bot_posts(ts) if p["text"].startswith("Already tracked")]
    replay = deliver(mention(event_id, ts, text=BOT + "task: replayed", user=USER))
    time.sleep(2)
    empty_id, empty_ts, _ = task_mention("task:")
    settled_inbox(empty_id)
    time.sleep(2)
    return {
        "a_repeat_task_in_the_thread_is_already_tracked_with_view_task": len(tracked)
        == 1
        and tracked[0]["text"].startswith("Already tracked in this thread: prepare")
        and (button(tracked[0]) or ("", ""))[0] == "View task"
        and button(tracked[0])[1].endswith(f"/#tasks/{task_id}")
        and len(tasks(org)) == before,
        "a_replayed_event_creates_no_second_task_or_post": replay["delivered"]
        and len(tasks(org)) == before
        and len(bot_posts(ts)) == 3,
        "an_empty_task_asks_for_the_text_and_creates_nothing": [
            p["text"] for p in bot_posts(empty_ts)
        ]
        == [MISSING_TEXT]
        and len(tasks(org)) == before,
    }


def refusal_results(a, agent):
    """Rule off, no default agent and the Execute limit: private, no task."""
    org = a["org_id"]
    base = len(tasks(org))
    results = {}
    rules = {
        "create_task": "off",
        "pr_review": "off",
        "run_workflow": "off",
        "ask_private": "off",
    }
    put_settings(a, rules=rules)
    off_id, off_ts, _ = task_mention("task: write the notes")
    settled_inbox(off_id)
    time.sleep(2)
    rules["create_task"] = "user_only"
    put_settings(a, rules=rules, default_task_agent_id=None)
    none_id, none_ts, _ = task_mention("task: write the notes")
    settled_inbox(none_id)
    time.sleep(2)
    put_settings(
        a,
        default_task_agent_id=agent["id"],
        execute_limits={"user_per_10_min": 1, "channel_per_10_min": 30},
    )
    limit_id, limit_ts, _ = task_mention("task: one task too many")
    settled_inbox(limit_id)
    time.sleep(2)
    put_settings(a, execute_limits={"user_per_10_min": 10, "channel_per_10_min": 30})
    unknown = put_settings(a, default_task_agent_id="no-such-agent")
    blocked = [e[0]["metadata"].get("reason") for e in executes(org, "blocked")]
    results["a_rule_that_is_off_is_refused_privately_with_scene_e"] = [
        w["text"] for w in whispers(off_ts)
    ] == [NOT_SET_UP] and not bot_posts(off_ts)
    results["no_default_agent_is_told_privately"] = [
        w["text"] for w in whispers(none_ts)
    ] == [NO_AGENT] and not bot_posts(none_ts)
    waits = [w["text"] for w in whispers(limit_ts)]
    results["the_execute_limit_is_told_privately_with_the_wait"] = (
        len(waits) == 1
        and waits[0].startswith(
            ":hourglass_flowing_sand: You're asking a lot; try again in"
        )
        and "rate_limit_user" in blocked
    )
    results["blocked_requests_create_no_task"] = len(tasks(org)) == base
    results["the_blocked_attempts_are_audited_without_text"] = (
        "rule_off" in blocked
        and MARKER not in json.dumps([e[0] for e in executes(org, "blocked")])
    )
    results["an_unknown_default_agent_is_rejected_by_the_settings_api"] = unknown == 422
    return results


def isolation_results(a):
    """Thread rows and task text belong to their organization alone."""
    mine = "SELECT count(*) FROM product_records WHERE collection = 'slack_threads'"
    leak = (
        "SELECT count(*) FROM product_records WHERE collection IN "
        "('slack_threads', 'audit_events') AND position(%s in payload::text) > 0"
    )
    return {
        "thread_rows_are_invisible_to_other_tenants_and_unscoped_roles": (
            scoped(mine, a["org_id"])[0][0] > 0
            and scoped(mine, "an-unrelated-org")[0][0] == 0
            and scoped(mine)[0][0] == 0
        ),
        "no_task_text_in_thread_rows_or_audit": rows(leak, MARKER)[0][0] == 0,
    }


def main():
    """Run the control and record it in the private fixture file."""
    require_disposable_database()
    reset_fake()
    a = tenant("p7t")
    provider = call(
        "POST",
        "/model-providers",
        a["admin"],
        {
            "provider_type": "openai_compatible",
            "display_name": a["marker"],
            "api_key": "fixture-model-p7t",
            "base_url": "https://models.tenancy.test/v1",
            "initial_model": "tenancy-fixture-model",
        },
    )
    if not provider or install(a).get("slack_install") != "ok":
        raise AssertionError("the setup for the task control did not complete")
    agent, ready = set_up(a)
    if not ready:
        raise AssertionError("the task agent, member and rule were not set up")
    results = creation_results(a, agent)
    first = results.pop("_first")
    results.update(repeat_results(a, first))
    results.update(refusal_results(a, agent))
    results.update(isolation_results(a))
    reset_fake()
    record(FIXTURE, results, CONTROL_ID)


if __name__ == "__main__":
    main()
