"""Shared helpers for the Slack bot controls (T7-I01 install, T7-I02 events).

Real stack, no testing endpoint: the install goes through the product's own
start and callback routes, events are delivered by the fake Slack server's
control endpoint (signed with the fixture secret, with Slack's retry and
three-second-ack model) to the running API, and rows are read with the same
database roles the product uses. Every token, code and secret here is made up.
"""

import json
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import psycopg

from tenancy_harness.client import request

FAKE = "http://provider:8081/slack"
EVENTS_URL = "http://api:8000/api/v0/slack/events"
TEAM = "T0FAKE001"
BOT_USER = "U0FAKEBOT"
CODE = "fixture-code"
TOKEN_MARK = "xoxb-fixture"
BOT_SCOPES = {
    "app_mentions:read",
    "chat:write",
    "channels:history",
    "groups:history",
    "channels:read",
    "groups:read",
    "reactions:read",
    "reactions:write",
}


def fake(method, path, body=None):
    """Call the fake Slack server's control surface (private network, no proxy)."""
    response = httpx.request(
        method, FAKE + path, json=body, timeout=30, trust_env=False
    )
    response.raise_for_status()
    return response.json()


def reset_fake():
    """Forget recorded calls and faults between checks."""
    fake("POST", "/_control/reset")


def fake_calls(method=None):
    """Recorded Web API calls, optionally of one method."""
    calls = fake("GET", "/_control/calls")["calls"]
    return [c for c in calls if method in (None, c["method"])]


def rows(query, *params):
    """Read durable rows on the disposable owner connection."""
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        return connection.execute(query, params).fetchall()


def scoped(query, org_id="", purpose=""):
    """Read as the restricted application role under one tenant or purpose."""
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        connection.execute(
            "SELECT set_config('role', 'weaves_app', true), "
            "set_config('app.org_id', %s, true), "
            "set_config('app.system_purpose', %s, true)",
            (org_id, purpose),
        )
        return connection.execute(query).fetchall()


def start_install(t):
    """The admin starts an install; returns (response, query of the URL)."""
    response = request("GET", "/oauth/slack/install", t["admin"]["headers"])
    query = {}
    if response.status_code == 200:
        url = response.json()["authorization_url"]
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
    return response, query


def install(t, code=CODE):
    """Run start then callback; returns the callback's redirect query."""
    _, query = start_install(t)
    done = request(
        "GET",
        "/oauth/slack/install/callback",
        {},
        params={"code": code, "state": query["state"]},
    )
    location = urlsplit(done.headers.get("location", ""))
    return {k: v[0] for k, v in parse_qs(location.query).items()}


def status(t):
    """The admin status view of the organization's Slack installations."""
    return request("GET", "/organization/slack", t["admin"]["headers"]).json()


def mention(event_id, ts, *, team=TEAM, text="<@U0FAKEBOT> what is open?"):
    """An ``app_mention`` event_callback body."""
    return {
        "type": "event_callback",
        "team_id": team,
        "event_id": event_id,
        "event": {
            "type": "app_mention",
            "user": "U0HUMAN01",
            "text": text,
            "ts": ts,
            "channel": "C0FAKE001",
        },
    }


def deliver(payload):
    """Deliver an event the way Slack does; returns the delivery report."""
    return fake(
        "POST", "/_control/deliver_event", {"url": EVENTS_URL, "payload": payload}
    )


def record(fixture_key, results, control_id):
    """Write the results into the private fixture file and print failures."""
    path = Path(os.environ["TENANCY_FIXTURE_FILE"])
    fixtures = json.loads(path.read_text())
    fixtures[fixture_key] = results
    path.write_text(json.dumps(fixtures))
    path.chmod(0o600)
    failed = [k for k, v in results.items() if v is not True and k != "details"]
    print(f"{control_id} recorded; failing checks: {failed or 'none'}")
    sys.stdout.flush()
