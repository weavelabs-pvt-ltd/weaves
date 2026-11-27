"""Offline planner calibration: recorded live replies against the strict parser.

``fixtures/planner_calibration.json`` holds question + tool list pairs and the
reply a real model (claude-haiku-5-5, through the budget-gated harness) gave to
the prompt whose hash is pinned below. Nothing here touches a network. Editing
the prompt makes the hash test fail: re-record before changing it.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from modules.slack.access.models import ToolHealth
from modules.slack.pipeline.models import DiscoveredTool, PlannerStatus
from modules.slack.pipeline.planner import PLANNER_MAX_OUTPUT_TOKENS, parse_plan
from modules.slack.pipeline.planner_prompt import SYSTEM_PROMPT, build_messages

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "planner_calibration.json").read_text()
)
RECORDS = FIXTURE["records"]
JIRA = "jira.issues.search"


def _tools(ids: list[str]) -> tuple[DiscoveredTool, ...]:
    catalog = FIXTURE["catalog"]
    return tuple(
        DiscoveredTool(
            i,
            "inst",
            "org",
            "L",
            catalog[i]["name"],
            catalog[i]["description"],
            i.split(".")[0],
            ToolHealth.HEALTHY,
        )
        for i in ids
    )


def test_the_recorded_replies_belong_to_the_current_prompt():
    digest = hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()
    assert digest == FIXTURE["prompt_sha256"], "prompt changed: re-run the calibration"


@pytest.mark.parametrize(
    "record", RECORDS, ids=[f"{r['id']}-{r['max_tokens']}" for r in RECORDS]
)
def test_each_recorded_reply_parses_as_recorded_and_as_expected(record):
    presented = frozenset(record["tools"])
    parsed = parse_plan(record["reply"], presented)
    assert parsed.status.value == record["parse"]
    assert list(parsed.needs) == record["needs"]
    if record["parse"] != "valid":
        # A truncated reply is refused whole: nothing from it is ever used.
        assert parsed.needs == () and record["stop_reason"] == "max_tokens"
        assert record["max_tokens"] < PLANNER_MAX_OUTPUT_TOKENS
        return
    expect = record["expect"]
    if expect == "tool":
        assert parsed.needs and not parsed.general_ok
    elif expect == "general":
        assert parsed.needs == () and parsed.general_ok
    elif expect == "no_jira":
        assert JIRA not in parsed.needs  # Jira was not offered and was asked for
    assert set(parsed.needs) <= presented


def test_the_live_run_had_no_strict_failure_at_the_shipped_output_cap():
    final = [r for r in RECORDS if r["max_tokens"] >= PLANNER_MAX_OUTPUT_TOKENS]
    assert len(final) >= 5
    for record in final:
        status = parse_plan(record["reply"], frozenset(record["tools"])).status
        assert status is PlannerStatus.VALID, record["id"]


def test_the_output_cap_leaves_room_for_the_models_own_reasoning():
    """Haiku 5.5 sometimes thinks first: up to 194 output tokens were used."""
    longest = max(r["output_tokens"] for r in RECORDS)
    assert PLANNER_MAX_OUTPUT_TOKENS >= 2 * longest > 0


def test_prompt_size_grows_with_the_tool_list_as_measured():
    sizes = {len(r["tools"]): r["input_tokens"] for r in RECORDS}
    assert sizes[12] > sizes[4]
    for record in RECORDS:
        system, user = build_messages(record["question"], _tools(record["tools"]))
        # A rough bound so a prompt rewrite cannot silently double the cost.
        estimate = (len(system.content) + len(user.content)) / 3
        assert record["input_tokens"] < estimate * 1.6
