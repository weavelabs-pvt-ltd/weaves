"""Opt-in live control T7-L01: a real Ask and a real Stop through the budget relay.

Never part of the required controls and never run in CI: it runs only with
``--live-anthropic-env ... --live-suite slack``. The fake Slack server is the
Slack side; the model side is the real Anthropic API reached **only** through the
budget relay and its persistent lifetime ledger (the product's provider adapter
talks to ``api.anthropic.com``, which on the disposable network is the fake
provider that forwards to the relay). The key stays inside the relay and the one
private config mount the existing live controls use; nothing here prints it.

Two paid calls, no more: (1) a short Ask is delivered as a signed event, answered
by the real model and posted exactly once (the working message, if one appeared,
becomes the receipt); (2) a long Ask is stopped from its real Stop button while the
model is still writing: the message becomes "Stopped.", the turn is cancelled, the
answer that the model then finishes is discarded and nothing is posted for it.
"""

import json
import os
import sys
from pathlib import Path

from tenancy_harness.budget import MAX_OUTPUT_TOKENS
from tenancy_harness.client import call
from tenancy_harness.phase6.controls_checks import until
from tenancy_harness.phase7.slack_ask_checks import flip
from tenancy_harness.phase7.slack_press_checks import patient_tenant, stopped_message
from tenancy_harness.phase7.slack_press_support import (
    ASKER,
    answers,
    ask,
    audit,
    bot_posts,
    buttons,
    new_series,
    press,
    runs_finished,
    thread_row,
    turn_status,
    wait_button,
)
from tenancy_harness.phase7.slack_support import install, reset_fake
from tenancy_harness.seed import require_disposable_database

CONTROL_ID = "T7-L01"
SHORT = "In one short sentence, what does idempotency mean for an API?"
LONG = (
    "Write a detailed, well structured essay of about 700 words on the history of "
    "the internet, from ARPANET to today."
)


def connect_real_model(a, config):
    """Disable the fixture models and register the real provider (priced)."""
    flip(a["org_id"], "disabled")
    model = config["solo_model"]
    provider = call(
        "POST",
        "/model-providers",
        a["admin"],
        {
            "provider_type": "anthropic",
            "display_name": "Live Slack",
            "api_key": config["api_key"],
            "initial_model": model,
            "base_url": "https://api.anthropic.com",
        },
    )
    profile = next(
        p
        for p in call("GET", "/model-profiles", a["admin"])
        if p["provider_id"] == provider["id"] and p["model"] == model
    )
    rate = config["rates"][model]
    call(
        "PATCH",
        "/model-profiles/" + profile["id"],
        a["admin"],
        {
            "input_cost_per_million_tokens_usd": rate["input_per_million_usd"],
            "output_cost_per_million_tokens_usd": rate["output_per_million_usd"],
        },
    )
    return model


def real_ask(a):
    """Call one: a signed mention, a real answer, posted once."""
    org = a["org_id"]
    _, ts, _ = ask(SHORT)
    posted = until(lambda: len(answers(ts)) >= 1, limit=150)
    until(lambda: (thread_row(org, ts) or {}).get("reply_state") == "posted", limit=60)
    runs_finished(org, limit=90)
    row = thread_row(org, ts) or {}
    return {
        "a_real_ask_is_answered_through_the_relay": posted is not None
        and row.get("run_state") == "done",
        "the_answer_is_posted_exactly_once": len(answers(ts)) == 1,
        "no_button_is_left_on_any_message": not any(buttons(m) for m in bot_posts(ts)),
    }


def real_stop(a):
    """Call two: Stop pressed from the real button while the model is writing."""
    org = a["org_id"]
    _, ts, _ = ask(LONG)
    found = wait_button(ts, "weaves.stop", limit=60)
    msg_ts, ref = found or (None, None)
    row = thread_row(org, ts) or {}
    before = turn_status(org, row.get("turn_id"))
    report = press(ASKER, "stop", ref, msg_ts, ts) if found else None
    stopped = until(lambda: stopped_message(ts), limit=60) is not None
    finished = runs_finished(org, limit=150)  # the model call ends; nothing follows
    after = thread_row(org, ts) or {}
    return {
        "the_stop_button_appeared_while_the_request_was_running": found is not None
        and before in ("running", "queued"),
        "the_press_was_acked_and_the_message_became_stopped": report is not None
        and report["delivered"]
        and stopped,
        "the_turn_is_cancelled_and_the_late_answer_is_discarded": finished
        and turn_status(org, row.get("turn_id")) == "cancelled"
        and after.get("run_state") == "stopped"
        and answers(ts) == [],
    }


def main():
    """Run both calls and write the private evidence file."""
    require_disposable_database()
    path = Path(os.environ["TENANCY_FIXTURE_FILE"])
    config = json.loads((path.parent / "live-config.json").read_text())
    if config["max_model_calls"] != 3 or MAX_OUTPUT_TOKENS < 1000:
        raise ValueError("Live Slack control needs the bounded relay configuration")
    new_series("7L", 1700009)  # T7-I08 used Ev7B... in this database
    reset_fake()
    a = patient_tenant("p7e")  # the organization T7-I08 left installed
    if install(a).get("slack_install") != "ok":  # a fresh token after the reset
        raise AssertionError("the Slack install for the live control failed")
    model = connect_real_model(a, config)
    results = real_ask(a)
    results.update(real_stop(a))
    passed = all(v is True for v in results.values())
    evidence = {
        "control": CONTROL_ID,
        "passed": passed,
        "model": model,
        "planned_model_calls": 2,
        "checks": results,
        "audited_presses": len(audit(a["org_id"])),
    }
    (path.parent / "live-slack-evidence.json").write_text(
        json.dumps(evidence, indent=2)
    )
    print(f"{CONTROL_ID} {'passed' if passed else 'FAILED'}; no automatic paid retry")
    sys.stdout.flush()
    if not passed:
        raise AssertionError(
            "the live Slack control failed; inspect the private evidence"
        )


if __name__ == "__main__":
    main()
