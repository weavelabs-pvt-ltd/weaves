"""Control T7-I03: the Slack Ask flow on the real stack (slice S4).

Slack is the fake server and the model is the fake provider (it echoes its prompt
and can be slowed down). Everything else is real: the signed event goes through the
running API into PostgreSQL, the worker's dispatch tick creates the hidden Q&A task
as the Slack service principal, the worker's guard and agent run it, and the reply
tick posts to the fake Slack with the bot token from the secret store.

Checked against the fake Slack and the database: a mention becomes exactly one
hidden private task (a service account holding only agents.run and tasks.create,
not listed for the admin), the answer is one new thread message with the evidence
line and the egress scrubber and mrkdwn sanitizer applied, a replayed event adds
nothing, a slow answer shows ONE working message edited in place and turned into
the receipt (two posts in all), a follow-up mention continues the same
conversation, the cheap exits (help, greeting, status, a missing PR link, a command
that is not allowed) create no task and count nothing against the Ask limits, a
planner that cannot name sources fails closed with a private "try again", the limit,
the paused bot and a missing AI model are told privately, and thread rows are
invisible to other tenants.
"""

import os
import time

import psycopg

from tenancy_harness.client import call, request
from tenancy_harness.phase6.controls_checks import until
from tenancy_harness.phase7.slack_support import (
    deliver,
    fake,
    fake_calls,
    install,
    mention,
    record,
    reset_fake,
    rows,
    scoped,
)
from tenancy_harness.resources import tenant
from tenancy_harness.seed import require_disposable_database

CONTROL_ID = "T7-I03"
SLICE = "S4"
FIXTURE = "slack_ask"
CHANNEL = "C0FAKE001"
ASKER = "U0HUMAN01"
BOT = "<@U0FAKEBOT> "
MARKER = "ZEBRA7731"
LEAK = "xoxb-fixture-leak-1234567890"
TS = iter(f"1700001{n:03d}.000100" for n in range(1, 999))
EVENTS = iter(f"Ev7A{n:03d}" for n in range(1, 999))
PATH = "/organization/slack/access"


def human_message(thread_ts=None):
    """A human message that exists in the fake channel; returns its ts."""
    ts = next(TS)
    message = {"channel": CHANNEL, "ts": ts, "text": "fixture", "thread_ts": thread_ts}
    fake("POST", "/_control/configure", {"messages": [message]})
    return ts


def ask(text, thread_ts=None):
    """Deliver a mention; returns (event id, the message ts, delivery report)."""
    ts = human_message(thread_ts)
    event_id = next(EVENTS)
    report = deliver(mention(event_id, ts, text=BOT + text, thread_ts=thread_ts))
    return event_id, ts, report


def threads(org_id):
    """The organization's thread rows (payloads), oldest first."""
    found = rows(
        "SELECT payload FROM product_records WHERE collection = 'slack_threads' "
        "AND org_id = %s ORDER BY created_at",
        org_id,
    )
    return [row[0] for row in found]


def settled(org_id, event_id, limit=90):
    """Wait until the request of ``event_id`` has nothing left to deliver."""

    def done():
        return any(
            t["event_id"] == event_id and t["reply_state"] != "pending"
            for t in threads(org_id)
        )

    return until(done, limit=limit) is not None


def inbox_state(event_id):
    """(state, text) of one inbox row."""
    found = rows(
        "SELECT state, text FROM slack_event_inbox WHERE event_id = %s", event_id
    )
    return found[0] if found else None


def settled_inbox(event_id, limit=30):
    """Wait until the dispatcher has settled one inbox row."""
    return (
        until(lambda: (inbox_state(event_id) or ("pending",))[0] != "pending", limit)
        is not None
    )


def state():
    """The fake Slack's messages and private messages."""
    return fake("GET", "/_control/state")


def bot_posts(thread_ts):
    """Messages the bot has in a thread (the working message and the answer)."""
    found = state()["messages"].get(CHANNEL, [])
    return [m for m in found if m["bot"] and m["thread_ts"] == thread_ts]


def whispers(thread_ts):
    """Private messages shown in a thread."""
    return [e for e in state()["ephemerals"] if e["thread_ts"] == thread_ts]


def task_count(org_id):
    """Hidden Q&A tasks of the organization."""
    return rows(
        "SELECT count(*) FROM product_records WHERE collection = 'tasks' "
        "AND org_id = %s AND idempotency_key LIKE 'slack:%%'",
        org_id,
    )[0][0]


def decisions(org_id):
    """Counted decision records (what the Ask limits are derived from)."""
    return rows(
        "SELECT count(*) FROM product_records WHERE collection = 'audit_events' "
        "AND org_id = %s AND payload->>'action' = 'slack.decision'",
        org_id,
    )[0][0]


def set_limits(a, per_user):
    """Replace the Ask limits through the admin API."""
    view = request("GET", PATH, a["admin"]["headers"]).json()
    body = {k: view[k] for k in view if k in FIELDS}
    body["ask_limits"] = {**view["ask_limits"], "user_per_10_min": per_user}
    body["expected_version"] = view["settings_version"]
    return request("PUT", PATH, a["admin"]["headers"], body).status_code == 200


FIELDS = {
    "ask_enabled",
    "connector_audience",
    "partial_answers",
    "planner_profile_id",
    "ask_limits",
    "rules",
    "workflow_ids",
    "pr_review_repos",
}


def answer_results(a):
    """A normal Ask: one hidden task, one new thread message, scrubbed and sanitized."""
    org = a["org_id"]
    question = f"explain idempotency {MARKER} {LEAK} and <!channel>"
    event_id, ts, _ = ask(question)
    ok = settled(org, event_id)
    (row,) = [t for t in threads(org) if t["event_id"] == event_id]
    posts = bot_posts(ts)
    task = rows(
        "SELECT payload->>'audience', payload->>'created_by_principal_id' "
        "FROM product_records WHERE collection = 'tasks' AND org_id = %s "
        "AND idempotency_key = %s",
        org,
        f"slack:T0FAKE001:{event_id}",
    )
    principal = rows(
        "SELECT payload->>'principal_type' FROM product_records "
        "WHERE collection = 'principals' AND record_id = %s",
        task[0][1] if task else "",
    )
    permissions = rows(
        "SELECT r.payload->'permissions' FROM product_records b "
        "JOIN product_records r ON r.collection = 'roles' "
        "AND r.record_id = b.payload->>'role_id' "
        "WHERE b.collection = 'role_bindings' AND b.org_id = %s "
        "AND b.payload->>'principal_id' = %s",
        org,
        task[0][1] if task else "",
    )
    listed = request("GET", "/tasks", a["admin"]["headers"]).json()
    text = posts[0]["text"] if posts else ""
    reactions = state()["reactions"]
    again = deliver(mention(event_id, ts, text=BOT + question))
    time.sleep(3)
    return {
        "the_mention_becomes_one_hidden_private_task": ok
        and task_count(org) == 1
        and task[0][0] == "private"
        and row["reply_state"] == "posted"
        and row["run_state"] == "done",
        "the_task_is_owned_by_a_service_account_with_two_permissions": bool(principal)
        and principal[0][0] == "service_account"
        and sorted(permissions[0][0]) == ["agents.run", "tasks.create"],
        "the_hidden_task_is_not_listed_for_the_admin": all(
            item["task_id"] != row["task_id"] for item in listed.get("items", [])
        ),
        "eyes_reaction_then_one_new_answer_in_the_thread": any(
            f"|{ts}|eyes|" in r or r.endswith(f"|{ts}|eyes") for r in reactions
        )
        and len(posts) == 1
        and "From general knowledge" in text,
        "the_answer_carries_the_evidence_and_timing_line": "_From general knowledge · "
        in text
        and text.rstrip().endswith("s_"),
        "the_egress_scrubber_and_sanitizer_hold": LEAK not in text
        and "<!channel>" not in text
        and "[slack_token removed]" in text,
        "the_inbox_row_is_settled_without_its_text": inbox_state(event_id)
        == ("done", None),
        "a_replayed_event_creates_no_second_task_or_answer": again["delivered"]
        and task_count(org) == 1
        and len(bot_posts(ts)) == 1,
        "the_request_id_is_slack_event": row["request_id"] == f"slack-{event_id}",
        "_first": (row["task_id"], ts),
    }


def working_results(a):
    """A slow answer: one working message, edited in place, then the receipt."""
    org = a["org_id"]
    before = len(fake_calls("chat.postMessage"))
    event_id, ts, _ = ask("a slow question tenancy-slow=14")
    seen = []

    def watch():
        for post in bot_posts(ts):
            if post["text"] not in seen:
                seen.append(post["text"])
        return any("From general knowledge" in p["text"] for p in bot_posts(ts))

    waited = until(watch, limit=60)
    ok = settled(org, event_id)
    posts = bot_posts(ts)
    mine = [
        c
        for c in fake_calls("chat.postMessage")[before:]
        if c["params"].get("thread_ts") == ts
    ]
    updates = [c for c in fake_calls("chat.update") if c["params"].get("text")]
    (row,) = [t for t in threads(org) if t["event_id"] == event_id]
    return {
        "a_slow_answer_shows_one_working_message_edited_in_place": waited is not None
        and any(t.startswith(":pencil2:") or t.startswith(":mag:") for t in seen)
        and len(updates) >= 1,
        "the_working_message_becomes_the_receipt": ok
        and len(posts) == 2
        and posts[0]["text"].startswith(":white_check_mark: Done in ")
        and "From general knowledge" in posts[1]["text"],
        "never_more_than_two_posts_per_request": len(mine) == 2 and row["posts"] == 2,
    }


def follow_up_results(a, first):
    """A second mention in the thread continues the same hidden conversation."""
    org = a["org_id"]
    task_id, ts = first
    before = task_count(org)
    event_id, _, _ = ask("and one more thing?", thread_ts=ts)
    ok = settled(org, event_id)
    (row,) = [t for t in threads(org) if t["thread_ts"] == ts]
    return {
        "a_follow_up_mention_continues_the_same_conversation": ok
        and row["task_id"] == task_id
        and row["request_count"] == 2
        and task_count(org) == before
        and len(bot_posts(ts)) >= 2,
    }


def cheap_results(a):
    """Help, greeting, status, a missing PR link and a command that is not allowed."""
    from modules.slack import copy

    org = a["org_id"]
    tasks, counted = task_count(org), decisions(org)
    sent = {}
    for name, text in (
        ("help", ""),
        ("hello", "hi"),
        ("status", "status"),
        ("review", "review"),
        ("task", "task: write the notes"),
    ):
        event_id, ts, _ = ask(text)
        settled_inbox(event_id)
        time.sleep(1.5)
        sent[name] = (ts, whispers(ts), bot_posts(ts))
    return {
        "help_and_greeting_are_threaded_replies": [p["text"] for p in sent["help"][2]]
        == [copy.HELP]
        and [p["text"] for p in sent["hello"][2]] == [copy.GREETING],
        "status_is_private": [w["text"] for w in sent["status"][1]]
        == [copy.NO_ACTIVITY]
        and not sent["status"][2],
        "a_missing_pr_link_asks_for_a_re_mention_with_an_example": [
            p["text"] for p in sent["review"][2]
        ]
        == [copy.MISSING_PR],
        "a_command_without_its_rule_is_refused_privately": [
            w["text"] for w in sent["task"][1]
        ]
        == [f":lock: {copy.NOT_SET_UP['task']}"]
        and not sent["task"][2],
        "cheap_exits_create_no_task_and_count_nothing": task_count(org) == tasks
        and decisions(org) == counted,
    }


def planner_results(a):
    """A planner that cannot name sources fails closed, privately."""
    from modules.slack.access import messages

    org = a["org_id"]
    call(
        "POST",
        "/plugin-installations",
        a["admin"],
        {
            "plugin_id": "mcp",
            "display_name": "Docs",
            "endpoint_url": "https://provider/mcp",
            "bearer_token": a["marker"],
            "read_only_tool_names": ["search_docs"],
            "catalog_connector_id": "custom-mcp",
        },
    )
    event_id, ts, _ = ask("what do the docs say about retention?")
    ok = settled(org, event_id)
    return {
        "a_failed_plan_says_try_again_privately_and_posts_nothing_public": ok
        and [w["text"] for w in whispers(ts)] == [messages.PLANNER_FAILED]
        and not bot_posts(ts),
    }


def refusal_results(a):
    """The limit, the paused bot and a missing AI model."""
    from modules.slack import copy
    from modules.slack.access import messages

    org = a["org_id"]
    tasks = task_count(org)
    results = {}
    limited = set_limits(a, 1)
    event_id, ts, _ = ask("one more please")
    settled_inbox(event_id)
    time.sleep(1.5)
    text = (whispers(ts) or [{"text": ""}])[0]["text"]
    results["the_ask_limit_is_told_privately_with_the_wait"] = (
        limited
        and text.startswith(
            ":hourglass_flowing_sand: You're asking a lot; try again in"
        )
        and task_count(org) == tasks
    )
    set_limits(a, 5)
    request("POST", PATH + "/pause", a["admin"]["headers"], {"paused": True})
    event_id, ts, _ = ask("are you there?")
    settled_inbox(event_id)
    time.sleep(1.5)
    results["a_paused_bot_says_it_is_unavailable"] = [
        w["text"] for w in whispers(ts)
    ] == [messages.UNAVAILABLE] and task_count(org) == tasks
    request("POST", PATH + "/pause", a["admin"]["headers"], {"paused": False})
    flip(org, "disabled")
    event_id, ts, _ = ask("what is idempotency?")
    settled_inbox(event_id)
    time.sleep(1.5)
    results["no_model_provider_is_told_privately"] = [
        w["text"] for w in whispers(ts)
    ] == [copy.NO_MODEL] and task_count(org) == tasks
    flip(org, "active")
    return results


def flip(org_id, status):
    """Disable or re-enable the organization's model profiles (disposable database)."""
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        connection.execute(
            "UPDATE product_records SET payload = jsonb_set(payload, '{status}', "
            "to_jsonb(%s::text)) WHERE collection = 'model_profiles' AND org_id = %s",
            (status, org_id),
        )


def isolation_results(a):
    """Thread rows and the Ask text belong to their organization alone."""
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
        "no_question_text_in_thread_rows_or_audit": rows(leak, MARKER)[0][0] == 0,
    }


def main():
    """Run the control and record it in the private fixture file."""
    require_disposable_database()
    reset_fake()
    a = tenant("p7a")
    provider = call(
        "POST",
        "/model-providers",
        a["admin"],
        {
            "provider_type": "openai_compatible",
            "display_name": a["marker"],
            "api_key": "fixture-model-p7a",
            "base_url": "https://models.tenancy.test/v1",
            "initial_model": "tenancy-fixture-model",
        },
    )
    if not provider or install(a).get("slack_install") != "ok":
        raise AssertionError("the setup for the Ask control did not complete")
    results = answer_results(a)
    first = results.pop("_first")
    results.update(working_results(a))
    results.update(follow_up_results(a, first))
    results.update(cheap_results(a))
    results.update(planner_results(a))
    results.update(refusal_results(a))
    results.update(isolation_results(a))
    reset_fake()
    record(FIXTURE, results, CONTROL_ID)


if __name__ == "__main__":
    main()
