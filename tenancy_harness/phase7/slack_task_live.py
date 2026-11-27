"""Opt-in live control: a real ``task:`` run end to end (never a required control).

Run only with ``--live-anthropic-env`` and ``--live-slack-task``: the product's own
provider client calls Anthropic through the budget relay (the same path as
``LIVE-I01``), and everything else is the stack of ``T7-I06`` with the fake Slack
server. ONE model call is planned and no other call exists: an Execute request is
planned by grammar, the agent lists no connector, and nothing retries.

The signed mention ``task: ...`` creates the task as the mapped member, the real
model answers in the worker, and the reply tick posts the result exactly once. The
module writes ``live-slack-task.json`` (the call's ledger evidence) beside the other
private live files; the host adds it to the relay-versus-ledger comparison.
"""

import json
import os
from pathlib import Path

from tenancy_harness.budget import MAX_OUTPUT_TOKENS
from tenancy_harness.client import call, request
from tenancy_harness.phase7.slack_support import deliver, install, mention, reset_fake
from tenancy_harness.phase7.slack_task_checks import (
    PATH,
    USER,
    bot_posts,
    human_message,
    put_settings,
    settled,
    tasks,
    threads,
)
from tenancy_harness.resources import tenant
from tenancy_harness.seed import require_disposable_database

MAX_CALLS = 1
LIVE_MARKER = "LIVE_SLACK_TASK"
WAIT_SECONDS = 240


def provider_and_agent(a, config):
    """A real Anthropic provider (priced like the relay) and a connector-free agent."""
    model = config["solo_model"]
    provider = call(
        "POST",
        "/model-providers",
        a["admin"],
        {
            "provider_type": "anthropic",
            "display_name": "Live slack task",
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
    agent = call(
        "POST",
        "/agents",
        a["admin"],
        {
            "name": "Ops assistant",
            "model_profile_id": profile["id"],
            "instructions": "For this synthetic check, put the exact marker "
            "supplied in the task on the first line, then one short plain "
            "sentence. Do not take actions.",
            "allowed_capability_ids": [],
            "max_output_tokens": MAX_OUTPUT_TOKENS,
            "max_runtime_seconds": 60,
            "max_tool_calls": 0,
        },
    )
    return model, agent


def ledger_rows(org_id):
    """The organization's usage ledger rows (state, tokens, cost, model)."""
    import psycopg

    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        return connection.execute(
            "SELECT state, input_tokens, output_tokens, estimated_cost_nano_usd, "
            "model_id FROM usage_ledger WHERE org_id = %s",
            (org_id,),
        ).fetchall()


def main():
    """Create one task from a signed mention and verify the single live call."""
    require_disposable_database()
    private = Path(os.environ["TENANCY_FIXTURE_FILE"]).parent
    config = json.loads((private / "live-config.json").read_text())
    reset_fake()
    a = tenant("p7live")
    if install(a).get("slack_install") != "ok":
        raise AssertionError("the live task control could not install the bot")
    model, agent = provider_and_agent(a, config)
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
    if (
        mapped.status_code != 200
        or put_settings(a, rules=rules, default_task_agent_id=agent["id"]) != 200
    ):
        raise AssertionError("the live task control could not set up the rule")
    event_id = "Ev7LIVE001"
    ts = human_message()
    text = f"<@U0FAKEBOT> task: Reply with {LIVE_MARKER} and one short sentence."
    deliver(mention(event_id, ts, text=text, user=USER))
    done = settled(a["org_id"], event_id, limit=WAIT_SECONDS)
    posts = bot_posts(ts)
    (made,) = tasks(a["org_id"])
    (row,) = threads(a["org_id"])
    rows = ledger_rows(a["org_id"])
    result = posts[1]["text"] if len(posts) == 2 else ""
    checks = {
        "the_task_was_created_as_the_mapped_member": made["created_by_principal_id"]
        == a["admin"]["principal_id"]
        and made["audience"] == "workspace",
        "the_result_was_posted_once_with_the_real_answer": done
        and len(posts) == 2
        and LIVE_MARKER in result
        and row["posts"] == 2,
        "exactly_one_settled_model_call_in_the_usage_ledger": len(rows) == 1
        and rows[0][0] == "settled",
        "the_key_never_reached_a_row_or_a_message": config["api_key"]
        not in json.dumps([posts, made, row]),
    }
    evidence = {
        "passed": all(checks.values()),
        "checks": checks,
        "planned_model_calls": MAX_CALLS,
        "calls": [
            {
                "model": model,
                "ledger_input_tokens": r[1],
                "ledger_output_tokens": r[2],
                "ledger_cost_nano_usd": r[3],
            }
            for r in rows
        ],
    }
    (private / "live-slack-task.json").write_text(json.dumps(evidence, indent=2))
    (private / "live-slack-task.json").chmod(0o600)
    failed = [k for k, v in checks.items() if not v]
    print(f"live slack task recorded; failing checks: {failed or 'none'}")
    reset_fake()
    if failed:
        raise AssertionError("the live task control failed; see the private evidence")


if __name__ == "__main__":
    main()
