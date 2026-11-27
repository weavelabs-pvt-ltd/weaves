"""Control T7-I08: Stop and Try again on the real stack (slice S4b).

Slack is the fake server and the model is the fake provider (slow on request, or
failing on request). Everything else is real: the signed interaction goes through
the running API into PostgreSQL, the worker's ``slack_interactions`` tick carries
it out, the button the press carries is the one the real bot posted, and the
cancellation goes through the product's own task cancellation path.

Checked: a signed press is acked inside three seconds and stored once (a replay is
the same press); a forged or stale signature is refused and stores nothing; a press
for an unknown workspace is acked and dropped; the working message of a slow
request carries a Stop button; another person is told privately and nothing
changes; an admin holding slack.manage may stop it; the requester's Stop turns the
message into "Stopped." without buttons, cancels the turn and no late answer is
ever posted; a second press says "Already stopped."; a forged reference or message
finds nothing; Stop works while the bot is paused; a failed request gets Try again,
which only the requester or an admin may press, runs the stored request again
through the gates (a paused bot refuses it) as a new turn that counts against the
Ask limits, and edits the failure into the working message; every press is audited
once with no text and no reference; the press rows lose their reference once
settled.
"""

import os
import time

import psycopg

from tenancy_harness.client import call, request
from tenancy_harness.phase6.controls_checks import until
from tenancy_harness.phase7.slack_ask_checks import FIELDS, PATH, flip
from tenancy_harness.phase7.slack_install_checks import no_token_anywhere
from tenancy_harness.phase7.slack_press_support import (
    ASKER,
    ack_ok,
    answers,
    ask,
    audit,
    bot_posts,
    buttons,
    find_button,
    model_fault,
    press,
    press_rows,
    runs_finished,
    thread_row,
    turn_status,
    wait_button,
    whispers,
)
from tenancy_harness.phase7.slack_support import (
    TEAM,
    fake,
    install,
    record,
    reset_fake,
    rows,
)
from tenancy_harness.resources import tenant
from tenancy_harness.seed import require_disposable_database

CONTROL_ID = "T7-I08"
SLICE = "S4b"
FIXTURE = "slack_interactions"
OTHER = "U0OTHER01"
ADMIN = "U0ADMIN01"
SLOW = "a slow question tenancy-slow=14 "
MARKER = "KUDU-PRESS-TEXT-5519"
# Presses the control carries out (replays and refused signatures excluded): 2 in
# the Stop control, 4 in the requester one, 1 ending the delivery one, 4 for Try again.
PRESSES = 11
ONLY_STOP = "Only the person who asked, or an admin, can stop this."
ONLY_RETRY = "Only the person who asked, or an admin, can retry this."
NO_LONGER = "That's no longer available."
UNAVAILABLE = "Weaves is unavailable right now. Try again later."


def texts(thread_ts):
    """Private message texts of a thread, in order."""
    return [w["text"] for w in whispers(thread_ts)]


def whispered(thread_ts, count):
    """Wait until a thread has ``count`` private messages."""
    until(lambda: len(whispers(thread_ts)) >= count, limit=30)
    return texts(thread_ts)


def running_request(a, extra=""):
    """A slow request with its Stop button: ``(ts, event id, button ts, ref)``."""
    event_id, ts, _ = ask(SLOW + extra)
    found = wait_button(ts, "weaves.stop")
    return ts, event_id, *(found or (None, None))


def stopped_message(thread_ts):
    """The thread's bot message that reads ``Stopped.`` with no buttons left."""
    return [
        m
        for m in bot_posts(thread_ts)
        if m["text"] == ":octagonal_sign: Stopped." and not buttons(m)
    ]


def stop_results(a):
    """Others are refused, an admin and the requester stop, no late answer."""
    org = a["org_id"]
    ts, _, msg_ts, ref = running_request(a, MARKER)
    row = thread_row(org, ts) or {}
    first = press(OTHER, "stop", ref, msg_ts, ts)
    other_texts = whispered(ts, 1)
    still = turn_status(org, row.get("turn_id")) in ("running", "queued")
    admin = press(ADMIN, "stop", ref, msg_ts, ts)
    stopped = until(lambda: stopped_message(ts), limit=30) is not None
    results = {
        "a_signed_press_is_acked_within_three_seconds": ack_ok(first) and ack_ok(admin),
        "the_working_message_carries_a_stop_button_with_an_opaque_reference": bool(ref)
        and len(ref) >= 20
        and all(
            ours not in ref
            for ours in (
                org,
                row.get("id", "x"),
                row.get("task_id", "x"),
                row.get("turn_id", "x"),
            )
        ),
        "another_person_is_told_privately_and_nothing_changes": other_texts
        == [ONLY_STOP]
        and still
        and whispers(ts)[0]["user"] == OTHER,
        "an_admin_holding_slack_manage_can_stop_it": stopped,
        "stop_turns_the_working_message_into_stopped_without_buttons": stopped
        and len(stopped_message(ts)) == 1,
    }
    done = runs_finished(org)
    after = thread_row(org, ts) or {}
    results["the_turn_is_cancelled_and_no_late_answer_is_posted"] = (
        done
        and turn_status(org, row.get("turn_id")) == "cancelled"
        and answers(ts) == []
        and after.get("run_state") == "stopped"
        and after.get("reply_state") == "posted"
    )
    return results


def requester_results(a):
    """The requester stops, presses twice, replays, forges; the bot is paused."""
    org = a["org_id"]
    ts, _, msg_ts, ref = running_request(a)
    forged = press(ASKER, "stop", "A" * 22, msg_ts, ts)
    wrong_message = press(ASKER, "stop", ref, "1700999999.000100", ts)
    got = whispered(ts, 2)
    row = thread_row(org, ts) or {}
    forged_ok = (
        ack_ok(forged)
        and ack_ok(wrong_message)
        and got == [NO_LONGER, NO_LONGER]
        and turn_status(org, row.get("turn_id")) in ("running", "queued")
    )
    request("POST", PATH + "/pause", a["admin"]["headers"], {"paused": True})
    rows_before = len(press_rows())
    pressed = press(ASKER, "stop", ref, msg_ts, ts, action_ts="1700004900.000100")
    replay = press(ASKER, "stop", ref, msg_ts, ts, action_ts="1700004900.000100")
    rows_after = len(press_rows())
    stopped = until(lambda: stopped_message(ts), limit=30) is not None
    request("POST", PATH + "/pause", a["admin"]["headers"], {"paused": False})
    again = press(ASKER, "stop", ref, msg_ts, ts)
    texts_now = whispered(ts, 3)
    runs_finished(org)
    return {
        "a_forged_reference_or_message_finds_nothing": forged_ok,
        "stop_works_while_the_bot_is_paused": ack_ok(pressed) and stopped,
        "a_replayed_press_is_one_press": ack_ok(replay)
        and rows_after == rows_before + 1,
        "a_double_press_says_already_stopped": ack_ok(again)
        and texts_now[-1] == "Already stopped."
        and len(stopped_message(ts)) == 1,
        "no_late_answer_after_a_stopped_request": answers(ts) == [],
    }


def delivery_results(a):
    """Forged and stale signatures, and an unknown workspace."""
    before = len(press_rows())
    ts, _, msg_ts, ref = running_request(a)
    after_running = len(press_rows())
    fake("POST", "/_control/configure", {"delivery_fault": "bad_signature"})
    forged = press(ASKER, "stop", ref, msg_ts, ts)
    fake("POST", "/_control/configure", {"delivery_fault": "stale_timestamp"})
    stale = press(ASKER, "stop", ref, msg_ts, ts)
    fake("POST", "/_control/configure", {"delivery_fault": None})
    unknown = press(ASKER, "stop", ref, msg_ts, ts, team="T0NOBODY1")
    row = thread_row(a["org_id"], ts) or {}
    results = {
        "a_forged_or_stale_signature_is_refused_and_stores_nothing": not forged[
            "delivered"
        ]
        and not stale["delivered"]
        and forged["attempts"][0]["status"] == 401
        and stale["attempts"][0]["status"] == 401
        and len(press_rows()) == after_running == before,
        "a_press_for_an_unknown_workspace_is_acked_and_dropped": ack_ok(unknown)
        and len(press_rows()) == before
        and turn_status(a["org_id"], row.get("turn_id")) in ("running", "queued"),
    }
    press(ASKER, "stop", ref, msg_ts, ts)  # end the request
    until(lambda: stopped_message(ts), limit=30)
    runs_finished(a["org_id"])
    return results


def failure_results(a):
    """A failed request gets Try again; the gates, the presser and the count."""
    org = a["org_id"]
    model_fault(500)
    try:
        event_id, ts, _ = ask("what is idempotency? " + MARKER)
        shown = wait_button(ts, "weaves.retry", limit=120)
    finally:
        model_fault(0)
    msg_ts, ref = shown or (None, None)
    row = thread_row(org, ts) or {}
    request("POST", PATH + "/pause", a["admin"]["headers"], {"paused": True})
    blocked = press(ASKER, "retry", ref, msg_ts, ts)
    paused_texts = whispered(ts, 1)
    request("POST", PATH + "/pause", a["admin"]["headers"], {"paused": False})
    stranger = press(OTHER, "retry", ref, msg_ts, ts)
    stranger_texts = whispered(ts, 2)
    turns_before = rows(
        "SELECT count(*) FROM product_records WHERE collection = 'task_turns' "
        "AND org_id = %s AND payload->>'task_id' = %s",
        org,
        row.get("task_id"),
    )[0][0]
    decisions_before = decisions(org)
    retried = press(ASKER, "retry", ref, msg_ts, ts)
    posted = until(lambda: len(answers(ts)) >= 1, limit=90)
    until(lambda: (thread_row(org, ts) or {}).get("run_state") == "done", limit=30)
    settled = thread_row(org, ts) or {}
    old_ref = press(ASKER, "retry", ref, msg_ts, ts)
    old_texts = whispered(ts, 3)
    keys = rows(
        "SELECT idempotency_key FROM product_records WHERE "
        "collection = 'task_turns' AND org_id = %s AND idempotency_key LIKE %s",
        org,
        "slack:%:retry:%",
    )
    return {
        "a_failed_request_offers_try_again": shown is not None
        and row.get("run_state") == "failed",
        "try_again_is_refused_by_the_gates_while_the_bot_is_paused": ack_ok(blocked)
        and paused_texts == [UNAVAILABLE],
        "try_again_by_someone_else_is_refused_privately": ack_ok(stranger)
        and stranger_texts[-1] == ONLY_RETRY
        and turns_before == 1,
        "try_again_runs_the_stored_request_as_a_new_turn_and_answers_once": ack_ok(
            retried
        )
        and posted is not None
        and len(answers(ts)) == 1
        and settled.get("retries") == 1
        and settled.get("run_state") == "done"
        and len(keys) == 1
        and keys[0][0].startswith("slack:T0FAKE001:retry:")
        and rows(
            "SELECT count(*) FROM product_records WHERE collection = 'task_turns' "
            "AND org_id = %s AND payload->>'task_id' = %s",
            org,
            row.get("task_id"),
        )[0][0]
        == 2,
        "the_failure_message_became_the_working_message_and_lost_its_button": any(
            "Done in" in m["text"] and not buttons(m) for m in bot_posts(ts)
        )
        and not find_button(ts, "weaves.retry"),
        "try_again_counts_against_the_ask_limits": decisions(org)
        == decisions_before + 1,
        "the_old_try_again_reference_is_dead_after_the_retry": ack_ok(old_ref)
        and old_texts[-1] == NO_LONGER,
        "_event": event_id,
    }


def decisions(org_id):
    """Counted decision records of the organization."""
    return rows(
        "SELECT count(*) FROM product_records WHERE collection = 'audit_events' "
        "AND org_id = %s AND payload->>'action' = 'slack.decision'",
        org_id,
    )[0][0]


def record_results(a):
    """One audit row per press without text; the press rows lose their reference."""
    org = a["org_id"]
    events = audit(org)
    metadata = [m for m, _ in events]
    allowed_reasons = {m["reason"] for m in metadata if m["outcome"] == "allowed"}
    blocked_reasons = {m["reason"] for m in metadata if m["outcome"] == "blocked"}
    dumped = " ".join(text for _, text in events)
    refs = rows(
        "SELECT payload->>'run_ref' FROM product_records WHERE collection = "
        "'slack_threads' AND org_id = %s",
        org,
    )
    delivered = [r for r in press_rows() if r[0] == "done"]
    return {
        "every_press_is_audited_once_with_its_reason_and_no_text": len(events)
        == PRESSES
        and {"requester", "admin", "already_stopped"} <= allowed_reasons
        and {"not_mapped", "no_run", "bot_paused"} <= blocked_reasons
        and MARKER not in dumped
        and not any(r[0] and r[0] in dumped for r in refs)
        and all(
            set(m)
            == {
                "action",
                "source",
                "outcome",
                "reason",
                "slack_user_id",
                "principal_id",
                "channel_id",
                "run",
            }
            for m in metadata
        ),
        "the_press_rows_lose_their_reference_once_settled": bool(delivered)
        and all(r[1] is None for r in press_rows() if r[0] != "pending"),
        "no_token_in_any_table_audit_or_ciphertext": no_token_anywhere(),
    }


def free_workspace():
    """Let the earlier controls' organization give the fake workspace back.

    Straight on the disposable database (the earlier organizations are not
    touched again: creating organizations through the API is rate limited, and
    every ``tenant()`` call counts).
    """
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        connection.execute(
            "UPDATE slack_installations SET status = 'uninstalled', "
            "revoked_at = now() WHERE team_id = %s AND status IN "
            "('active', 'suspended')",
            (TEAM,),
        )


def patient_tenant(label):
    """``tenant()``, waiting out the organization-creation rate limit."""
    for _ in range(12):
        try:
            return tenant(label)
        except AssertionError as error:
            if "429" not in str(error):
                raise
            time.sleep(15)
    return tenant(label)


def setup(a):
    """The bot installed, a model connected, the admin mapped, limits relaxed.

    A fresh organization with no connector: nothing it asks needs the planner, so
    the only model call of a request is the answer (and the fault of the failure
    case hits that call, which fails the turn).
    """
    # The fake was just reset, so the bot token an earlier control left in the
    # database is unknown to it: a reinstall (in place) issues a fresh one.
    if install(a).get("slack_install") != "ok":
        raise AssertionError("the Slack install for the interactions control failed")
    # Every new organization has a local assistant profile: turn all profiles off
    # first, so the fixture provider created next is the only model the Q&A agent can
    # use (a request then really waits on the fake model, which can be slowed).
    flip(a["org_id"], "disabled")
    call(
        "POST",
        "/model-providers",
        a["admin"],
        {
            "provider_type": "openai_compatible",
            "display_name": a["marker"],
            "api_key": "fixture-model-p7i",
            "base_url": "https://models.tenancy.test/v1",
            "initial_model": "tenancy-fixture-model",
        },
    )
    call(
        "POST",
        PATH + "/people",
        a["admin"],
        {
            "slack_user_id": ADMIN,
            "principal_id": a["admin"]["principal_id"],
            "note": "",
        },
    )
    view = request("GET", PATH, a["admin"]["headers"]).json()
    body = {k: view[k] for k in view if k in FIELDS}
    body["ask_limits"] = {
        **view["ask_limits"],
        "user_per_10_min": 100,
        "channel_per_10_min": 200,
    }
    body["expected_version"] = view["settings_version"]
    if request("PUT", PATH, a["admin"]["headers"], body).status_code != 200:
        raise AssertionError("could not relax the Ask limits")


def main():
    """Run the control and record it in the private fixture file."""
    require_disposable_database()
    reset_fake()
    free_workspace()
    a = patient_tenant("p7e")  # an organization with no connector
    setup(a)
    results = stop_results(a)
    results.update(requester_results(a))
    results.update(delivery_results(a))
    failure = failure_results(a)
    failure.pop("_event")
    results.update(failure)
    results.update(record_results(a))
    reset_fake()
    record(FIXTURE, results, CONTROL_ID)


if __name__ == "__main__":
    main()
