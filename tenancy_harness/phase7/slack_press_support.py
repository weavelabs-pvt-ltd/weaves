"""Helpers for the Stop and Try again controls (T7-I07 and the opt-in T7-L01).

Real stack: the signed interaction is delivered by the fake Slack server's
``deliver_interaction`` (form-encoded ``payload=``, signed with the fixture
secret, three-second ack model) to the running API; the buttons are read from the
messages the real bot posted into the fake Slack, so the reference a press
carries is the one the product issued. Every id, token and secret here is made up.
"""

import httpx

from tenancy_harness.phase6.controls_checks import until
from tenancy_harness.phase7.slack_support import (
    TEAM,
    deliver,
    fake,
    mention,
    rows,
)

INTERACTIONS_URL = "http://api:8000/api/v0/slack/interactions"
CHANNEL = "C0FAKE001"
ASKER = "U0HUMAN01"
BOT = "<@U0FAKEBOT> "
ACK_LIMIT = 3.0
ANSWER = "From general knowledge"
TS = iter(f"1700003{n:03d}.000100" for n in range(1, 999))
EVENTS = iter(f"Ev7B{n:03d}" for n in range(1, 999))
ACTION_TS = iter(f"1700004{n:03d}.000100" for n in range(1, 999))


def new_series(tag, base):
    """Start fresh message, event and action ids (another control, same database)."""
    global TS, EVENTS, ACTION_TS
    TS = iter(f"{base}{n:03d}.000100" for n in range(1, 999))
    EVENTS = iter(f"Ev{tag}{n:03d}" for n in range(1, 999))
    ACTION_TS = iter(f"{base + 1}{n:03d}.000100" for n in range(1, 999))


def human_message(thread_ts=None):
    """A human message that exists in the fake channel; returns its ts."""
    ts = next(TS)
    message = {"channel": CHANNEL, "ts": ts, "text": "fixture", "thread_ts": thread_ts}
    fake("POST", "/_control/configure", {"messages": [message]})
    return ts


def ask(text, thread_ts=None, user=ASKER):
    """Deliver a mention; returns (event id, the message ts, delivery report)."""
    ts = human_message(thread_ts)
    event_id = next(EVENTS)
    payload = mention(event_id, ts, text=BOT + text, thread_ts=thread_ts, user=user)
    return event_id, ts, deliver(payload)


def state():
    """The fake Slack's messages and private messages."""
    return fake("GET", "/_control/state")


def bot_posts(thread_ts):
    """Messages the bot has in a thread (the working message, the answer, ...)."""
    found = state()["messages"].get(CHANNEL, [])
    return [m for m in found if m["bot"] and m["thread_ts"] == thread_ts]


def whispers(thread_ts):
    """Private messages shown in a thread."""
    return [e for e in state()["ephemerals"] if e["thread_ts"] == thread_ts]


def answers(thread_ts):
    """The bot's answer posts of a thread (not the working message)."""
    return [m for m in bot_posts(thread_ts) if ANSWER in m["text"]]


def buttons(message):
    """``(action_id, value)`` pairs of the buttons a message carries."""
    return [
        (e["action_id"], e["value"])
        for block in message.get("blocks") or []
        if block["type"] == "actions"
        for e in block["elements"]
    ]


def find_button(thread_ts, action_id):
    """``(message ts, value)`` of the newest message with that button, or None."""
    for message in reversed(bot_posts(thread_ts)):
        for found, value in buttons(message):
            if found == action_id:
                return message["ts"], value
    return None


def wait_button(thread_ts, action_id, limit=60):
    """Wait for the bot to show a button; returns ``(message ts, value)`` or None."""
    until(lambda: find_button(thread_ts, action_id), limit=limit)
    return find_button(thread_ts, action_id)


def press(user, action, value, message_ts, thread_ts, *, team=TEAM, action_ts=None):
    """Deliver one button press the way Slack does; returns the delivery report."""
    payload = {
        "type": "block_actions",
        "team": {"id": team},
        "user": {"id": user},
        "container": {
            "type": "message",
            "message_ts": message_ts,
            "channel_id": CHANNEL,
        },
        "channel": {"id": CHANNEL},
        "message": {"ts": message_ts, "thread_ts": thread_ts},
        "actions": [
            {
                "type": "button",
                "action_id": f"weaves.{action}",
                "block_id": "weaves.run",
                "value": value,
                "action_ts": action_ts or next(ACTION_TS),
            }
        ],
    }
    return fake(
        "POST",
        "/_control/deliver_interaction",
        {"url": INTERACTIONS_URL, "payload": payload},
    )


def ack_ok(report):
    """Delivered on the only attempt, inside Slack's three-second window."""
    first = report["attempts"][0]
    return report["delivered"] and first["ack_seconds"] < ACK_LIMIT


def press_rows():
    """Interaction rows of the inbox: (state, text, drop reason)."""
    return rows(
        "SELECT state, text, drop_reason FROM slack_event_inbox "
        "WHERE event_type = 'interaction' ORDER BY received_at"
    )


def audit(org_id):
    """The org's ``slack.interaction`` audit metadata, oldest first."""
    found = rows(
        "SELECT payload->'metadata', payload::text FROM product_records "
        "WHERE collection = 'audit_events' AND org_id = %s "
        "AND payload->>'action' = 'slack.interaction' ORDER BY created_at",
        org_id,
    )
    return [(row[0], row[1]) for row in found]


def thread_row(org_id, thread_ts):
    """The thread row (payload) of one Slack thread, or None."""
    found = rows(
        "SELECT payload FROM product_records WHERE collection = 'slack_threads' "
        "AND org_id = %s AND payload->>'thread_ts' = %s",
        org_id,
        thread_ts,
    )
    return found[0][0] if found else None


def turn_status(org_id, turn_id):
    """Status of one task turn."""
    found = rows(
        "SELECT payload->>'status' FROM product_records WHERE collection = "
        "'task_turns' AND org_id = %s AND record_id = %s",
        org_id,
        turn_id,
    )
    return found[0][0] if found else None


def runs_finished(org_id, limit=90):
    """Wait until no agent run of the organization is queued or running."""

    def idle():
        return (
            rows(
                "SELECT count(*) FROM product_records WHERE org_id = %s "
                "AND collection = 'agent_runs' "
                "AND payload->>'status' IN ('queued', 'running')",
                org_id,
            )[0][0]
            == 0
        )

    return until(idle, limit=limit) is not None


def model_fault(status):
    """Make every model call of the fake provider fail with ``status`` (0 clears)."""
    httpx.post(
        "http://provider:8081/model-fault",
        json={"status": status},
        timeout=10,
        trust_env=False,
    ).raise_for_status()
