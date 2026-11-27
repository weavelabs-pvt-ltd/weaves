"""Control T7-I05: a PR review in Slack on the real stack (slice S6).

Slack, GitHub and the model are fakes (``fake_slack``, ``fake_github``,
``fake_review_model``); everything else is real: the signed mention goes through
the running API into PostgreSQL, the worker's dispatch tick checks the pre-flight
(rule, mapped member, repository covered and allowlisted), creates a private task
for the mapped member, the worker's guard approves exactly the pull request reader,
the reader fetches the recorded pull request from the fake GitHub with a token
limited to one repository, the review model answers (a *fooled* one when the pull
request carries the injection attempt) and the reply tick posts to the fake Slack.

Checked: the first message names the pull request and files and becomes the
receipt, the review is a second message in the thread format (risk icon and word,
findings with file names in code style, one question, the "Nothing was posted on
GitHub" footer), the injected instruction and a fooled model cannot mention anyone
or link out, GitHub is only ever read inside the pull request and never written
to, a 41-file pull request is a partial review, an unreadable pull request, a
model answer that breaks the contract and a repository that is not allowlisted
each get their fixed sentence (the last with no GitHub call and no task), a bad
link asks for the PR link, a replayed event adds nothing, and no pull request text
is in an audit event or a thread row.
"""

import json
import os
import re
import time
from datetime import UTC, datetime

import httpx
import psycopg

from tenancy_harness.client import call, request
from tenancy_harness.phase6.controls_checks import until
from tenancy_harness.phase7.slack_support import (
    BOT_USER,
    deliver,
    fake,
    install,
    mention,
    record,
    reset_fake,
    rows,
    scoped,
    status,
)
from tenancy_harness.resources import tenant
from tenancy_harness.seed import require_disposable_database

CONTROL_ID = "T7-I05"
SLICE = "S6"
FIXTURE = "slack_review"
CHANNEL = "C0FAKE001"
ASKER = "U0HUMAN01"
BOT = f"<@{BOT_USER}> "
TEAM = "T0FAKE001"
TS = iter(f"1700002{n:03d}.000100" for n in range(1, 999))
EVENTS = iter(f"Ev7R{n:03d}" for n in range(1, 999))
GITHUB = "http://provider:8081/github"
PATH = "/organization/slack/access"
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
URL = "https://github.com/acme/api/pull/{}"
INJECTION = "PWNED-7731"
TITLE = "Add retry to payments client"
UNREADABLE = (
    "I can't read that PR. Check the link, or ask a Weaves admin to allow this "
    "repository."
)
NO_REVIEW = "I couldn't produce a review. Try again."
MISSING = (
    "Reply with @Weaves and the PR link, for example "
    "@Weaves review https://github.com/acme/api/pull/42"
)


def github(path):
    """The fake GitHub's control surface (private network, no proxy)."""
    response = httpx.get(GITHUB + path, timeout=30, trust_env=False)
    response.raise_for_status()
    return response.json()


def reset_github():
    """Forget recorded GitHub requests."""
    github("/_control/reset")


def human_message():
    """A human message that exists in the fake channel; returns its ts."""
    ts = next(TS)
    message = {"channel": CHANNEL, "ts": ts, "text": "fixture", "thread_ts": None}
    fake("POST", "/_control/configure", {"messages": [message]})
    return ts


def say(text):
    """Deliver a mention; returns (event id, the message ts)."""
    ts = human_message()
    event_id = next(EVENTS)
    deliver(mention(event_id, ts, text=BOT + text))
    return event_id, ts


def threads(org_id):
    """The organization's thread rows (payloads), oldest first."""
    found = rows(
        "SELECT payload FROM product_records WHERE collection = 'slack_threads' "
        "AND org_id = %s ORDER BY created_at",
        org_id,
    )
    return [row[0] for row in found]


def settled(org_id, event_id, limit=120):
    """Wait until the request of ``event_id`` has nothing left to deliver."""
    return (
        until(
            lambda: any(
                t["event_id"] == event_id and t["reply_state"] != "pending"
                for t in threads(org_id)
            ),
            limit=limit,
        )
        is not None
    )


def settled_inbox(event_id, limit=60):
    """Wait until the dispatcher has settled one inbox row."""

    def state():
        found = rows(
            "SELECT state FROM slack_event_inbox WHERE event_id = %s", event_id
        )
        return bool(found) and found[0][0] != "pending"

    return until(state, limit) is not None


def posts(thread_ts):
    """The bot's messages in a thread, oldest first."""
    state = fake("GET", "/_control/state")
    found = state["messages"].get(CHANNEL, [])
    return [m for m in found if m["bot"] and m["thread_ts"] == thread_ts]


def whispers(thread_ts):
    """Private messages shown in a thread."""
    return [
        e
        for e in fake("GET", "/_control/state")["ephemerals"]
        if e["thread_ts"] == thread_ts
    ]


def tasks(org_id):
    """Task rows made for Slack events of the organization."""
    return rows(
        "SELECT payload FROM product_records WHERE collection = 'tasks' "
        "AND org_id = %s AND idempotency_key LIKE 'slack:%%'",
        org_id,
    )


def review_posts(thread_ts):
    """The review messages (not the working message or receipt) of a thread."""
    return [p for p in posts(thread_ts) if p["text"].startswith("*Review of ")]


def settings(a, *, repos):
    """Turn the review rule on for the listed asker and allowlist ``repos``."""
    view = request("GET", PATH, a["admin"]["headers"]).json()
    body = {k: view[k] for k in view if k in FIELDS}
    body["rules"] = {**view["rules"], "pr_review": "user_only"}
    body["pr_review_repos"] = repos
    body["expected_version"] = view["settings_version"]
    changed = request("PUT", PATH, a["admin"]["headers"], body).status_code == 200
    mapped = request(
        "POST",
        PATH + "/people",
        a["admin"]["headers"],
        {"slack_user_id": ASKER, "principal_id": a["admin"]["principal_id"]},
    ).status_code in (200, 201)
    return changed and mapped


def connect_github(a):
    """A GitHub App connection that predates the review capability (a stored row)."""
    from contracts.v1 import PluginInstallation, PluginInstallationStatus

    now = datetime.now(UTC)
    installation = PluginInstallation(
        plugin_installation_id="gh-t7r-" + a["org_id"][:8],
        org_id=a["org_id"],
        workspace_id=a["workspace_id"],
        plugin_id="github",
        display_name="GitHub",
        configuration={
            "auth_method": "github_app",
            "github_installation_id": "424242",
            "repositories": ["acme/api", "acme/private"],
        },
        enabled_capability_ids=("github.issues.search", "github.code.search"),
        status=PluginInstallationStatus.ACTIVE,
        created_at=now,
        updated_at=now,
    )
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        connection.execute(
            "INSERT INTO product_records (collection, record_id, org_id, "
            "workspace_id, payload) VALUES ('plugin_installations', %s, %s, %s, %s)",
            (
                installation.plugin_installation_id,
                a["org_id"],
                a["workspace_id"],
                json.dumps(installation.model_dump(mode="json")),
            ),
        )
    return installation.plugin_installation_id


def review(a, number, repo="acme/api"):
    """Ask for a review and wait for it; returns (event id, ts, thread row)."""
    event_id, ts = say("review " + URL.format(number).replace("acme/api", repo))
    done = settled(a["org_id"], event_id)
    rowset = [t for t in threads(a["org_id"]) if t["event_id"] == event_id]
    return event_id, ts, (rowset[0] if rowset and done else None)


def normal_results(a):
    """The injection-bearing pull request #123 and a model that has been fooled."""
    org = a["org_id"]
    reset_github()
    event_id, ts, row = review(a, 123)
    texts = [p["text"] for p in posts(ts)]
    answer = next((t for t in texts if t.startswith("*Review of ")), "")
    receipt = [t for t in texts if t.startswith(":white_check_mark:")]
    calls = github("/_control/calls")
    gets = [
        c["path"]
        for c in calls["calls"]
        if c["method"] == "GET" and "/pulls/" in c["path"]
    ]
    token_calls = [c for c in calls["calls"] if c["method"] == "POST"]
    (task,) = [
        t[0]
        for t in tasks(org)
        if t[0]["idempotency_key"] == f"slack:{TEAM}:{event_id}"
    ]
    turn = rows(
        "SELECT payload FROM product_records WHERE collection = 'task_turns' "
        "AND org_id = %s AND payload->>'task_id' = %s",
        org,
        task["task_id"],
    )[0][0]
    counted = len(tasks(org))
    again = deliver(mention(event_id, ts, text=BOT + "review " + URL.format(123)))
    time.sleep(3)
    return {
        "the_review_is_one_message_in_the_scene_d_format": row is not None
        and len(review_posts(ts)) == 1
        and answer.startswith("*Review of acme/api#123*")
        and ":large_orange_circle: *Medium risk*" in answer
        and "• `payments/client.py`: Retries are unbounded on 5xx responses." in answer
        and "*Question for the author:* " in answer
        and "Reviewed 14 of 14 files · Nothing was posted on GitHub · <" in answer,
        "a_working_message_becomes_the_receipt_when_one_was_posted": not receipt
        or (
            len(receipt) == 1
            and re.fullmatch(
                r":white_check_mark: Reviewed acme/api#123, "
                rf'"{TITLE}" · 14 of 14 files · \d+s',
                receipt[0],
            )
            is not None
        ),
        "never_more_than_two_bot_posts_per_request": row is not None
        and len(posts(ts)) <= 2
        and row["posts"] <= 2,
        "a_fooled_model_cannot_mention_or_link_out": "<!channel>" not in answer
        and "<@U0ADMIN>" not in answer
        and "<https://evil.test" not in answer
        and not whispers(ts),
        "the_task_is_private_and_owned_by_the_mapped_member": task["audience"]
        == "private"
        and task["created_by_principal_id"] == a["admin"]["principal_id"]
        and turn["origin"]["action"] == "pr_review"
        and turn["origin"]["repo"] == "acme/api",
        "the_task_text_is_built_from_the_validated_link": rows(
            "SELECT payload->>'content' FROM product_records WHERE "
            "collection = 'task_messages' AND org_id = %s AND "
            "payload->>'role' = 'user' AND payload->>'task_id' = %s",
            org,
            task["task_id"],
        )[0][0]
        == "Review pull request " + URL.format(123),
        "github_is_only_read_inside_the_pull_request": gets
        == ["/repos/acme/api/pulls/123", "/repos/acme/api/pulls/123/files"]
        and calls["unexpected"] == []
        and len(token_calls) >= 1
        and all(c.get("repositories") == ["api"] for c in token_calls),
        "a_replayed_event_posts_nothing_more": again["delivered"]
        and len(review_posts(ts)) == 1
        and len(tasks(org)) == counted,
        "the_reader_was_enabled_on_the_connection_and_audited": bool(
            rows(
                "SELECT 1 FROM product_records WHERE collection = 'audit_events' "
                "AND org_id = %s AND payload->>'action' = "
                "'slack.review.capability_enabled'",
                org,
            )
        ),
    }


def working_results(a):
    """A slow model: one working message naming the PR, edited into the receipt."""
    _, ts, row = review(a, 200)
    seen = []

    def watch():
        for post in posts(ts):
            if post["text"] not in seen:
                seen.append(post["text"])
        return len(seen) >= 2

    until(watch, limit=5)
    final = [p["text"] for p in posts(ts)]
    return {
        "a_slow_review_shows_the_pull_request_while_it_runs": any(
            t.startswith(":mag: Reviewing acme/api#200") for t in seen
        )
        or (len(final) == 2 and final[0].startswith(":white_check_mark: Reviewed")),
        "the_slow_review_ends_as_receipt_then_review": row is not None
        and len(final) == 2
        and final[0].startswith(":white_check_mark: Reviewed acme/api#200,")
        and "· 1 of 1 files ·" in final[0]
        and final[1].startswith("*Review of acme/api#200*"),
    }


def partial_and_failure_results(a):
    """A 41-file PR, an unreadable PR, a broken model answer."""
    _, ts41, row41 = review(a, 41)
    _, ts404, row404 = review(a, 404)
    _, ts429, row429 = review(a, 429)
    _, ts300, row300 = review(a, 300)
    text41 = (review_posts(ts41) or [{"text": ""}])[0]["text"]
    return {
        "a_big_pull_request_is_a_partial_review": row41 is not None
        and "_Reviewed 9 of 41 files (skipped generated and lock files) · Nothing was"
        in text41,
        "an_unreadable_pull_request_says_so_in_one_message": row404 is not None
        and [p["text"] for p in posts(ts404)] == [UNREADABLE]
        and "internal secret text" not in json.dumps(posts(ts404)),
        "a_rate_limited_github_says_try_again_later": row429 is not None
        and [p["text"] for p in posts(ts429)]
        == ["GitHub is limiting requests right now. Try again in a few minutes."],
        "a_model_answer_that_breaks_the_contract_fails_closed": row300 is not None
        and [p["text"] for p in posts(ts300)] == [NO_REVIEW]
        and INJECTION not in json.dumps(posts(ts300)),
    }


def preflight_results(a):
    """A repository that is not allowlisted, a bad link, a missing link."""
    org = a["org_id"]
    reset_github()
    before = len(tasks(org))
    event_id, ts = say("review https://github.com/acme/private/pull/5")
    settled_inbox(event_id)
    until(lambda: whispers(ts), limit=30)
    private_calls = github("/_control/calls")["calls"]
    ids = {}
    for name, text in (
        ("hostile", "review https://github.com.evil.test/acme/api/pull/123"),
        ("missing", "review"),
        ("two", "review " + URL.format(1) + " " + URL.format(2)),
    ):
        event, hts = say(text)
        settled_inbox(event)
        until(lambda hts=hts: posts(hts), limit=30)
        ids[name] = hts
    return {
        "a_repository_that_is_not_allowlisted_is_refused_privately": [
            w["text"] for w in whispers(ts)
        ]
        == [":lock: That repository isn't enabled for Slack."]
        and not posts(ts)
        and len(tasks(org)) == before,
        "a_refused_review_makes_no_github_call": private_calls == [],
        "a_missing_or_bad_link_asks_for_the_pr_link_with_an_example": all(
            [p["text"] for p in posts(ids[name])] == [MISSING] for name in ids
        )
        and len(tasks(org)) == before,
    }


def privacy_results(a):
    """No pull request text in audit events or thread rows; tenant isolation."""
    org = a["org_id"]
    leak = (
        "SELECT count(*) FROM product_records WHERE collection IN "
        "('slack_threads', 'audit_events') AND (position(%s in payload::text) > 0 "
        "OR position(%s in payload::text) > 0 OR position(%s in payload::text) > 0)"
    )
    mine = "SELECT count(*) FROM product_records WHERE collection = 'slack_threads'"
    decisions = rows(
        "SELECT count(*) FROM product_records WHERE collection = 'audit_events' "
        "AND org_id = %s AND payload->>'action' = 'slack.decision' "
        "AND payload->'metadata'->>'action' = 'pr_review'",
        org,
    )[0][0]
    return {
        "no_pull_request_text_in_audit_or_thread_rows": rows(
            leak, TITLE, INJECTION, "payments/client.py"
        )[0][0]
        == 0,
        "every_review_decision_is_audited_without_text": decisions >= 5,
        "review_rows_are_invisible_to_other_tenants": scoped(mine, org)[0][0] > 0
        and scoped(mine, "an-unrelated-org")[0][0] == 0
        and scoped(mine)[0][0] == 0,
    }


def free_the_workspace():
    """The Ask control's organization still holds the fake workspace: disconnect it."""
    old = tenant("p7a")
    for item in status(old).get("installations", []):
        if item["status"] == "active":
            request(
                "DELETE",
                "/organization/slack/" + item["installation_id"],
                old["admin"]["headers"],
            )


def main():
    """Run the control and record it in the private fixture file."""
    require_disposable_database()
    reset_fake()
    free_the_workspace()
    a = tenant("p7r")
    provider = call(
        "POST",
        "/model-providers",
        a["admin"],
        {
            "provider_type": "openai_compatible",
            "display_name": a["marker"],
            "api_key": "fixture-model-p7r",
            "base_url": "https://models.tenancy.test/v1",
            "initial_model": "tenancy-fixture-model",
        },
    )
    if not provider or install(a).get("slack_install") != "ok":
        raise AssertionError("the setup for the review control did not complete")
    connect_github(a)
    if not settings(a, repos=["acme/api"]):
        raise AssertionError("the review settings could not be saved")
    results = normal_results(a)
    results.update(working_results(a))
    results.update(partial_and_failure_results(a))
    results.update(preflight_results(a))
    results.update(privacy_results(a))
    reset_fake()
    record(FIXTURE, results, CONTROL_ID)


if __name__ == "__main__":
    main()
