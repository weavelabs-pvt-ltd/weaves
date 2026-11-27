"""Control T7-I05: Slack reaction feedback on the real stack (slice S4c).

Slack is the fake server and the model is the fake provider. Everything else is
real: the signed ``reaction_added`` events go through the running API into the
inbox, the worker's ``slack_feedback`` tick settles them, and PostgreSQL holds
the ``slack_feedback`` rows under row-level security.

Checked: the asker's thumbs up on our answer becomes exactly one rating that
joins to its turn, task and request id, with the asker only as a keyed hash and no
text; a later thumbs down (with a skin tone) replaces it, so the latest wins and
there is still one row; the same event delivered again, another user's reaction,
a reaction on a message that is not our answer, our own reaction and any other
emoji change nothing; the rating is invisible to another tenant and to an
unscoped role, and neither the row nor the audit trail holds the question text
or the raw Slack user id.
"""

import os

import psycopg

from tenancy_harness.client import request
from tenancy_harness.phase6.controls_checks import until
from tenancy_harness.phase7.slack_support import (
    BOT_USER,
    TEAM,
    deliver,
    fake,
    install,
    mention,
    record,
    reset_fake,
    rows,
    scoped,
)
from tenancy_harness.resources import tenant
from tenancy_harness.seed import require_disposable_database

CONTROL_ID = "T7-I05"
SLICE = "S4c"
FIXTURE = "slack_feedback"
CHANNEL = "C0FEED001"
ASKER = "U0FEEDASK1"
OTHER = "U0FEEDOTH1"
MARKER = "QUOKKA5519"
TS = iter(f"1700002{n:03d}.000100" for n in range(1, 999))
DIAGNOSIS = {}
EVENTS = iter(f"Ev7F{n:03d}" for n in range(1, 999))


def reaction_event(event_id, ts, *, reaction="+1", user=ASKER):
    """A ``reaction_added`` event_callback body on one message of the channel."""
    return {
        "type": "event_callback",
        "team_id": TEAM,
        "event_id": event_id,
        "event": {
            "type": "reaction_added",
            "user": user,
            "reaction": reaction,
            "item": {"type": "message", "channel": CHANNEL, "ts": ts},
            "event_ts": next(TS),
        },
    }


def without_connectors(org_id):
    """Disable the organization's connectors (the Ask control leaves one).

    With a connector offered the fake model cannot plan, which is the Ask
    control's own case; this control needs a model-only answer, posted publicly.
    """
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        connection.execute(
            "UPDATE product_records SET payload = jsonb_set(payload, '{status}', "
            "'\"disabled\"') WHERE collection = 'plugin_installations' "
            "AND org_id = %s",
            (org_id,),
        )


def thread_for(org_id, event_id):
    """The thread row of one request, or None."""
    found = rows(
        "SELECT payload FROM product_records WHERE collection = 'slack_threads' "
        "AND org_id = %s AND payload->>'event_id' = %s",
        org_id,
        event_id,
    )
    return found[0][0] if found else None


def ask(org_id, text):
    """Ask as ASKER and wait for the answer; returns (user message ts, thread row)."""
    ts = next(TS)
    fake(
        "POST",
        "/_control/configure",
        {"messages": [{"channel": CHANNEL, "ts": ts, "text": "fixture"}]},
    )
    event_id = next(EVENTS)
    body = mention(event_id, ts, text=f"<@{BOT_USER}> {text}", user=ASKER)
    body["event"]["channel"] = CHANNEL
    report = deliver(body)
    DIAGNOSIS["event"] = (event_id, report.get("delivered"))

    def answered():
        row = thread_for(org_id, event_id)
        return row is not None and row["reply_state"] == "posted"

    until(answered, limit=90)
    return ts, thread_for(org_id, event_id)


def ratings(org_id):
    """The organization's stored ratings (payloads), oldest first."""
    found = rows(
        "SELECT payload FROM product_records WHERE collection = 'slack_feedback' "
        "AND org_id = %s ORDER BY created_at",
        org_id,
    )
    return [row[0] for row in found]


def inbox(event_id):
    """(state, drop reason) of one inbox row, or None when none was stored."""
    found = rows(
        "SELECT state, drop_reason FROM slack_event_inbox WHERE event_id = %s",
        event_id,
    )
    return found[0] if found else None


def react(ts, **fields):
    """Deliver one reaction; returns (event id, delivery report)."""
    event_id = next(EVENTS)
    return event_id, deliver(reaction_event(event_id, ts, **fields))


def settled(event_id):
    """Wait until the worker settled one inbox row; returns its state and reason."""
    until(lambda: (inbox(event_id) or ("pending",))[0] != "pending", limit=45)
    return inbox(event_id)


def rating_results(org_id, answer_ts, thread):
    """The asker's thumbs up, then a thumbs down with a skin tone."""
    up_id, _ = react(answer_ts)
    until(lambda: len(ratings(org_id)) == 1, limit=45)
    if not ratings(org_id):
        pending = rows(
            "SELECT event_id, state, drop_reason FROM slack_event_inbox "
            "WHERE event_type = 'reaction_added'"
        )
        raise AssertionError(
            f"no rating was stored: inbox={inbox(up_id)} all={pending} "
            f"answer_ts={thread.get('answer_ts')}"
        )
    (first,) = ratings(org_id)
    down_id, _ = react(answer_ts, reaction="-1::skin-tone-3")
    until(lambda: ratings(org_id)[:1] and ratings(org_id)[0]["value"] == -1, limit=45)
    after = ratings(org_id)
    return {
        "the_askers_thumbs_up_on_our_answer_is_one_rating": first.get("value") == 1
        and first.get("source") == "slack"
        and inbox(up_id) == ("done", None),
        "the_rating_joins_to_its_turn_task_and_request": first.get("turn_id")
        == thread["turn_id"]
        and first.get("task_id") == thread["task_id"]
        and first.get("request_id") == thread["request_id"]
        and first.get("answer_ref") == f"{CHANNEL}:{answer_ts}"
        and bool(first.get("agent_run_id")),
        "the_rating_keeps_the_agent_model_and_connector_snapshot": bool(
            first.get("agent_id")
        )
        and bool(first.get("model_id"))
        and "connector_set" in first,
        "the_asker_is_a_keyed_hash_not_the_slack_id": first.get("asker_kind")
        == "hashed"
        and str(first.get("asker_ref", "")).startswith("hmac:")
        and ASKER not in str(first),
        "a_later_thumbs_down_replaces_it_newest_wins": len(after) == 1
        and after[0]["value"] == -1
        and inbox(down_id) == ("done", None),
    }


def ignored_results(org_id, answer_ts, user_ts):
    """Reactions that must change nothing."""
    same_id, _ = react(answer_ts, reaction="+1")
    settled(same_id)
    until(lambda: ratings(org_id)[0]["value"] == 1, limit=45)
    before = ratings(org_id)
    replay = deliver(reaction_event(same_id, answer_ts))
    other_id, _ = react(answer_ts, user=OTHER)
    elsewhere_id, _ = react(user_ts)
    own_id, _ = react(answer_ts, user=BOT_USER, reaction="-1")
    tada_id, _ = react(answer_ts, reaction="tada")
    other_state = settled(other_id)
    elsewhere_state = settled(elsewhere_id)
    after = ratings(org_id)
    return {
        "a_redelivered_event_is_stored_once": replay["delivered"]
        and rows("SELECT count(*) FROM slack_event_inbox WHERE event_id = %s", same_id)[
            0
        ][0]
        == 1
        and len(after) == 1,
        "another_users_reaction_changes_nothing": other_state
        == ("dropped", "not_the_asker"),
        "a_reaction_on_a_message_that_is_not_our_answer_changes_nothing": (
            elsewhere_state == ("dropped", "not_our_answer")
        ),
        "our_own_reaction_and_other_emoji_are_never_stored": inbox(own_id) is None
        and inbox(tada_id) is None,
        "ignored_reactions_leave_the_rating_as_it_was": len(before) == 1
        and after == before,
    }


def final_results(org_id, answer_ts):
    """The thumbsup alias with a skin tone, the settled inbox, the pepper."""
    alias_id, _ = react(answer_ts, reaction="thumbsup::skin-tone-5")
    settled(alias_id)
    after = ratings(org_id)
    return {
        "the_thumbsup_alias_with_a_skin_tone_counts_as_a_thumbs_up": len(after) == 1
        and after[0]["value"] == 1
        and inbox(alias_id) == ("done", None),
        "reaction_inbox_rows_are_settled_without_text": rows(
            "SELECT count(*) FROM slack_event_inbox WHERE event_type = "
            "'reaction_added' AND org_id = %s AND (state = 'pending' OR "
            "text IS NOT NULL)",
            org_id,
        )[0][0]
        == 0,
        "the_pepper_lives_in_the_secret_store_once": rows(
            "SELECT count(*) FROM product_secrets WHERE org_id = %s AND "
            "purpose = 'slack_feedback_pepper'",
            org_id,
        )[0][0]
        == 1,
    }


def isolation_results(org_id):
    """Row security, and nothing but ids in the rating or the audit trail."""
    mine = "SELECT count(*) FROM product_records WHERE collection = 'slack_feedback'"
    leak = (
        "SELECT count(*) FROM product_records WHERE collection = 'slack_feedback' "
        "AND (position(%s in payload::text) > 0 OR position(%s in payload::text) > 0)"
    )
    audit_leak = (
        "SELECT count(*) FROM product_records WHERE collection = 'audit_events' "
        "AND org_id = %s AND position(%s in payload::text) > 0"
    )
    return {
        "the_rating_is_invisible_to_other_tenants_and_unscoped_roles": scoped(
            mine, org_id
        )[0][0]
        == 1
        and scoped(mine, "an-unrelated-org")[0][0] == 0
        and scoped(mine)[0][0] == 0,
        "no_question_text_or_raw_slack_id_in_the_rating": rows(leak, MARKER, ASKER)[0][
            0
        ]
        == 0,
        "no_question_text_in_the_audit_trail_and_no_feedback_audit_noise": rows(
            audit_leak, org_id, MARKER
        )[0][0]
        == 0
        and rows(
            "SELECT count(*) FROM product_records WHERE collection = 'audit_events' "
            "AND org_id = %s AND payload->>'action' LIKE 'slack.feedback%%'",
            org_id,
        )[0][0]
        == 0,
    }


def main():
    """Run the control and record it in the private fixture file."""
    require_disposable_database()
    reset_fake()
    fake(
        "POST",
        "/_control/configure",
        {"channels": [{"id": CHANNEL, "name": "feedback"}]},
    )
    a = tenant("p7a")
    if not rows(
        "SELECT 1 FROM product_records WHERE collection = 'model_providers' "
        "AND org_id = %s",
        a["org_id"],
    ):  # normally left by the Ask control; make the control runnable alone
        request(
            "POST",
            "/model-providers",
            a["admin"]["headers"],
            {
                "provider_type": "openai_compatible",
                "display_name": a["marker"],
                "api_key": "fixture-model-p7f",
                "base_url": "https://models.tenancy.test/v1",
                "initial_model": "tenancy-fixture-model",
            },
        )
    if install(a).get("slack_install") != "ok":
        raise AssertionError("the setup for the feedback control did not complete")
    org = a["org_id"]
    without_connectors(org)
    user_ts, thread = ask(org, f"explain idempotency {MARKER}")
    if thread is None or thread.get("answer_ts") in (None, "", "private"):
        shown = {
            k: (thread or {}).get(k)
            for k in ("run_state", "reply_state", "reply_error")
        }
        event_id = DIAGNOSIS.get("event", ("", None))[0]
        whispers = fake("GET", "/_control/state")["ephemerals"][-2:]
        raise AssertionError(
            f"the question was not answered: {shown} {DIAGNOSIS} "
            f"inbox={inbox(event_id)} whispers={whispers}"
        )
    results = rating_results(org, thread["answer_ts"], thread)
    results.update(ignored_results(org, thread["answer_ts"], user_ts))
    results.update(final_results(org, thread["answer_ts"]))
    results.update(isolation_results(org))
    reset_fake()
    record(FIXTURE, results, CONTROL_ID)


if __name__ == "__main__":
    main()
