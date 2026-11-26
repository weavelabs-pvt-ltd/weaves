"""Control T7-I02: the signed Events endpoint and inbox on the real stack (slice S2).

Slack is the fake server: it signs with the fixture secret, measures the ack
(two xx within three seconds) and retries as Slack does. Checked against the
running API and PostgreSQL: ``url_verification`` echo, forged and stale
signatures refused with no inbox row, unknown teams acked and dropped, a mention
stored once whatever the retries and parallel deliveries, the ``:eyes:`` reaction
added after the ack (and never blocking it, even when Slack's API fails or is
slow), organization suspension and the platform pause dropping events without
text, bot-token revocation and uninstall ending the installation, and the inbox
being invisible to every tenant.
"""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

from clients.slack.signature import compute_signature
from tenancy_harness.phase6.controls_checks import control, lifecycle, until
from tenancy_harness.phase7.slack_support import (
    BOT_USER,
    EVENTS_URL,
    TEAM,
    deliver,
    fake,
    fake_calls,
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

CONTROL_ID = "T7-I02"
SLICE = "S2"
FIXTURE = "slack_events"
OP = ["--operator", "harness@tenancy.test", "--reason", "T7-I02 control"]
ACK_LIMIT = 3.0
TS = iter(f"1700000{n:03d}.000100" for n in range(100, 999))


def next_ts():
    """A fresh message timestamp that also exists in the fake's channel."""
    ts = next(TS)
    message = {"channel": "C0FAKE001", "ts": ts, "text": "fixture"}
    fake("POST", "/_control/configure", {"messages": [message]})
    return ts


def inbox(event_id):
    """The inbox row of one event as the owner sees it, or None."""
    found = rows(
        "SELECT state, drop_reason, text, attempts, org_id, request_id "
        "FROM slack_event_inbox WHERE event_id = %s",
        event_id,
    )
    return found[0] if found else None


def acked(report, retries=0):
    """Delivered on the first attempt, inside Slack's three-second window."""
    first = report["attempts"][0]
    return (
        report["delivered"]
        and len(report["attempts"]) == retries + 1
        and first["ack_seconds"] < ACK_LIMIT
    )


def reacted(ts):
    """Wait for the ``:eyes:`` reaction on message ``ts`` to reach the fake."""
    return (
        until(
            lambda: any(
                f"|{ts}|eyes|" in r or r.endswith(f"|{ts}|eyes")
                for r in fake("GET", "/_control/state")["reactions"]
            ),
            limit=10,
        )
        is not None
    )


def signature_results():
    """Verification, forged and stale requests, and unknown teams."""
    verification = deliver({"type": "url_verification", "challenge": "t7-i02"})
    results = {
        "url_verification_is_echoed": verification["delivered"]
        and verification["challenge_echoed"] is True
    }
    for fault, key in (
        ("bad_signature", "forged_signature_is_refused_and_stores_nothing"),
        ("stale_timestamp", "stale_timestamp_is_refused_and_stores_nothing"),
    ):
        fake("POST", "/_control/configure", {"delivery_fault": fault})
        event_id = f"Ev7{fault[:3].upper()}"
        report = deliver(mention(event_id, next_ts()))
        fake("POST", "/_control/configure", {"delivery_fault": None})
        retried = [
            a["sent_headers"].get("x-slack-retry-num") for a in report["attempts"]
        ]
        results[key] = (
            not report["delivered"]
            and {a["status"] for a in report["attempts"]} == {401}
            and retried == [None, "1", "2", "3"]
            and inbox(event_id) is None
        )
    unknown = deliver(mention("Ev7NOBODY", next_ts(), team="T0NOBODY"))
    results["unknown_team_is_acked_and_dropped"] = acked(unknown) and not rows(
        "SELECT 1 FROM slack_event_inbox WHERE team_id = 'T0NOBODY'"
    )
    return results


def mention_results(a):
    """A mention is stored once and acknowledged with :eyes: after the ack."""
    ts = next_ts()
    report = deliver(mention("Ev7ONE", ts))
    row = inbox("Ev7ONE")
    again = deliver(mention("Ev7ONE", ts))
    other_id = deliver(mention("Ev7TWO", ts))
    headers = retry_headers("Ev7ONE", ts)
    storm_ts = next_ts()
    with ThreadPoolExecutor(max_workers=8) as pool:
        storm = list(
            pool.map(lambda _: deliver(mention("Ev7STORM", storm_ts)), range(8))
        )
    return {
        "mention_is_acked_within_three_seconds_and_stored_pending": acked(report)
        and row is not None
        and row[:2] == ("pending", None)
        and row[2] == "<@U0FAKEBOT> what is open?"
        and row[4] == a["org_id"]
        and row[5] == "slack-Ev7ONE",
        "eyes_reaction_is_added_after_the_ack": reacted(ts),
        "a_repeated_event_id_is_stored_once": acked(again)
        and rows("SELECT count(*) FROM slack_event_inbox WHERE event_id = 'Ev7ONE'")[0][
            0
        ]
        == 1,
        "slack_retry_headers_are_deduplicated": headers == 200
        and inbox("Ev7ONE")[3] == 1,
        "one_message_under_two_event_ids_is_one_row": acked(other_id)
        and inbox("Ev7TWO") is None,
        "parallel_deliveries_store_one_row_and_one_reaction": all(
            acked(r) for r in storm
        )
        and rows("SELECT count(*) FROM slack_event_inbox WHERE event_id = 'Ev7STORM'")[
            0
        ][0]
        == 1
        and reacted(storm_ts)
        and sum(
            1
            for c in fake_calls("reactions.add")
            if c["params"].get("timestamp") == storm_ts
        )
        == 1,
    }


def retry_headers(event_id, ts):
    """POST a duplicate with Slack's retry headers, signed with the fixture secret."""
    body = json.dumps(mention(event_id, ts)).encode()
    stamp = str(int(time.time()))
    secret = os.environ["WEAVES_SLACK_SIGNING_SECRET"].encode()
    response = httpx.post(
        EVENTS_URL,
        content=body,
        headers={
            "x-slack-request-timestamp": stamp,
            "x-slack-signature": compute_signature(secret, stamp, body),
            "x-slack-retry-num": "2",
            "x-slack-retry-reason": "http_timeout",
            "content-type": "application/json",
        },
        timeout=10,
        trust_env=False,
    )
    return response.status_code


def slack_failure_results():
    """A failing or slow Slack API never blocks or fails the ack."""
    fake(
        "POST",
        "/_control/configure",
        {"api_fault": {"kind": "500", "method": "reactions.add", "count": 1}},
    )
    failing = deliver(mention("Ev7FAIL", next_ts()))
    fake(
        "POST",
        "/_control/configure",
        {
            "api_fault": {
                "kind": "slow",
                "method": "reactions.add",
                "count": 1,
                "delay_seconds": 5,
            }
        },
    )
    slow = deliver(mention("Ev7SLOW", next_ts()))
    fake("POST", "/_control/configure", {"api_fault": None})
    return {
        "a_failing_slack_api_does_not_fail_the_ack": acked(failing)
        and inbox("Ev7FAIL")[0] == "pending",
        "a_slow_slack_api_does_not_delay_the_ack": acked(slow)
        and slow["attempts"][0]["ack_seconds"] < 1.5
        and inbox("Ev7SLOW")[0] == "pending",
    }


def organization_state_results(a):
    """Suspension and the platform pause drop events and keep no text."""
    lifecycle("suspend", "--organization-id", a["org_id"], *OP)
    suspended = deliver(mention("Ev7SUSP", next_ts()))
    lifecycle("resume", "--organization-id", a["org_id"], *OP)
    resumed = deliver(mention("Ev7BACK", next_ts()))
    control("set", *OP, "--admissions-paused", "true")
    seen = {}

    def dropped():
        event_id = f"Ev7PAUSE{len(seen)}"
        deliver(mention(event_id, next_ts()))
        seen[event_id] = inbox(event_id)
        return seen[event_id] is not None and seen[event_id][0] == "dropped"

    waited = until(dropped)
    control("clear", *OP)
    last = list(seen.values())[-1] if seen else None

    def accepted_again():
        event_id = f"Ev7CLEAR{len(seen)}"
        seen[event_id] = None
        deliver(mention(event_id, next_ts()))
        seen[event_id] = inbox(event_id)
        return seen[event_id] is not None and seen[event_id][0] == "pending"

    # The switch has a short cache: later controls must not start before it is off.
    cleared = until(accepted_again)
    return {
        "the_platform_pause_clears_without_a_restart": cleared is not None,
        "a_suspended_organization_drops_events_without_text": acked(suspended)
        and inbox("Ev7SUSP")[:3] == ("dropped", "org_unavailable", None),
        "a_resumed_organization_accepts_events_again": acked(resumed)
        and inbox("Ev7BACK")[0] == "pending",
        "the_platform_pause_drops_events_without_text": waited is not None
        and last[:3] == ("dropped", "platform_paused", None),
    }


def lifecycle_results(a):
    """A bot-token revocation, a reinstall and an uninstall."""
    user_only = deliver(
        {
            "type": "event_callback",
            "team_id": TEAM,
            "event_id": "Ev7USERTOK",
            "event": {
                "type": "tokens_revoked",
                "tokens": {"oauth": ["U0X"], "bot": []},
            },
        }
    )
    still_active = status(a)["installations"][0]["status"] == "active"
    revoked = deliver(
        {
            "type": "event_callback",
            "team_id": TEAM,
            "event_id": "Ev7BOTTOK",
            "event": {"type": "tokens_revoked", "tokens": {"bot": [BOT_USER]}},
        }
    )
    (view,) = status(a)["installations"]
    reinstalled = install(a).get("slack_install") == "ok"
    gone = deliver(
        {
            "type": "event_callback",
            "team_id": TEAM,
            "event_id": "Ev7UNINST",
            "event": {"type": "app_uninstalled"},
        }
    )
    (after,) = status(a)["installations"]
    later = deliver(mention("Ev7LATE", next_ts()))
    return {
        "revoking_only_user_tokens_leaves_the_bot_alone": acked(user_only)
        and still_active,
        "bot_token_revocation_marks_the_installation_revoked": acked(revoked)
        and (view["status"], view["needs_reinstall"]) == ("revoked", True),
        "a_revoked_workspace_reinstalls_in_place": reinstalled,
        "app_uninstalled_ends_it_and_deletes_the_token": acked(gone)
        and after["status"] == "uninstalled"
        and not rows(
            "SELECT 1 FROM product_secrets WHERE purpose = 'slack_bot_token' "
            "AND org_id = %s",
            a["org_id"],
        ),
        "events_after_uninstall_are_acked_and_dropped": acked(later)
        and inbox("Ev7LATE") is None,
    }


def isolation_results(a):
    """The inbox belongs to the slack purpose alone."""
    count = "SELECT count(*) FROM slack_event_inbox"
    return {
        "inbox_is_invisible_to_tenants_and_unscoped_roles": scoped(count, a["org_id"])[
            0
        ][0]
        == 0
        and scoped(count)[0][0] == 0
        and scoped(count, purpose="slack")[0][0] > 0,
    }


def main():
    """Run the control and record it in the private fixture file."""
    require_disposable_database()
    reset_fake()
    a = tenant("p7e")
    if install(a).get("slack_install") != "ok":
        raise AssertionError("the install for the events control did not complete")
    try:
        results = signature_results()
        results.update(mention_results(a))
        results.update(slack_failure_results())
        results.update(organization_state_results(a))
        results.update(isolation_results(a))
        results.update(lifecycle_results(a))
    finally:
        control("clear", *OP)
        reset_fake()
    record(FIXTURE, results, CONTROL_ID)


if __name__ == "__main__":
    main()
