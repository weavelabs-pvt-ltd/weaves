"""Regex hints that name a source the question obviously needs.

Hints are unioned into the plan, so a Jira key or a pull request link can never
be answered from model knowledge when the planner misses it. A hint only ever
points at tools the organization already has; it grants nothing.
"""

from __future__ import annotations

import re

from modules.slack.pipeline.models import DiscoveredTool

_JIRA_KEY = re.compile(r"\b([A-Z][A-Z0-9_]{1,49})-[1-9][0-9]{0,8}\b")
_PR_URL = re.compile(r"https?://github\.com/[\w.-]+/[\w.-]+/pull/\d+", re.IGNORECASE)


def hinted_tool_ids(
    question: str, tools: tuple[DiscoveredTool, ...]
) -> tuple[str, ...]:
    """Tool ids a Jira key, a PR link or a connected repository name points at."""
    lowered = question.lower()
    keys = {match.group(1) for match in _JIRA_KEY.finditer(question)}
    has_pr_url = _PR_URL.search(question) is not None
    found: list[str] = []
    for tool in tools:
        if (tool.plugin_type.startswith("jira") and _names_project(keys, tool)) or (
            tool.plugin_type == "github"
            and (has_pr_url or any(repo in lowered for repo in tool.repositories))
        ):
            found.append(tool.tool_id)
    return tuple(dict.fromkeys(found))


def _names_project(keys: set[str], tool: DiscoveredTool) -> bool:
    if not keys:
        return False
    # With configured projects only a key of those projects counts ("UTF-8" is
    # not a ticket); without them any ticket-shaped key does.
    return not tool.projects or bool(keys & tool.projects)
