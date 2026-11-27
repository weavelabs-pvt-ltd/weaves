"""Fixed user-facing texts (design 3.5). None of them names a list member.

Every text is a constant or built from the asker's own situation (a wait time or
a connector's display name). The engine never interpolates a Slack id, a member
name, a note or the contents of an allowlist.
"""

from __future__ import annotations

from math import ceil

from modules.slack.access.models import Reason

UNAVAILABLE = "Weaves is unavailable right now. Try again later."
READ_ONLY = "Weaves is read-only right now. Try again later."
ASK_DISABLED = (
    "Asking Weaves from Slack isn't turned on for this workspace. Ask a Weaves admin."
)
RULE_OFF = (
    "Running things from Slack isn't turned on for this workspace. Ask a Weaves admin."
)
USER_NOT_LISTED = "You're not on the list for this from Slack. Ask a Weaves admin."
CHANNEL_NOT_LISTED = "This isn't enabled in this channel. Ask a Weaves admin."
NOT_MAPPED = (
    "Your Slack account isn't connected to a Weaves member. Ask a Weaves admin."
)
NO_PERMISSION = "Your Weaves role doesn't allow this. Ask a Weaves admin."
PLANNER_FAILED = "I couldn't work out which sources to check. Try again."
TARGET_MISSING = "I need a link to a pull request or the name of a workflow for that."
REPO_NOT_ENABLED = "That repository isn't enabled for Slack."
WORKFLOW_NOT_ENABLED = "That workflow isn't enabled for Slack."

_HEALTH_WORDS = {
    Reason.TOOL_UNAVAILABLE: "it's unavailable",
    Reason.TOOL_DISABLED: "it's turned off",
    Reason.TOOL_TOKEN_EXPIRED: "its connection expired",
    Reason.TOOL_QUARANTINED: "it's temporarily unavailable",
}

_ACCESS = (Reason.AUDIENCE_NOT_IN_SLACK, Reason.AUDIENCE_NOT_ALLOWED)

_FIXED: dict[Reason, str | None] = {
    Reason.SLACK_DISABLED: None,
    Reason.INSTALLATION_INACTIVE: None,
    Reason.PLATFORM_PAUSED: UNAVAILABLE,
    Reason.ORG_UNAVAILABLE: UNAVAILABLE,
    Reason.BOT_PAUSED: UNAVAILABLE,
    Reason.READ_ONLY: READ_ONLY,
    Reason.ASK_DISABLED: ASK_DISABLED,
    Reason.RULE_OFF: RULE_OFF,
    Reason.USER_NOT_LISTED: USER_NOT_LISTED,
    Reason.CHANNEL_NOT_LISTED: CHANNEL_NOT_LISTED,
    Reason.MEMBER_NOT_MAPPED: NOT_MAPPED,
    Reason.MEMBER_INACTIVE: NOT_MAPPED,
    Reason.PERMISSION_MISSING: NO_PERMISSION,
    Reason.PLANNER_FAILED: PLANNER_FAILED,
    Reason.TARGET_MISSING: TARGET_MISSING,
    Reason.REPO_NOT_COVERED: REPO_NOT_ENABLED,
    Reason.REPO_NOT_ALLOWED: REPO_NOT_ENABLED,
    Reason.WORKFLOW_NOT_PUBLISHED: WORKFLOW_NOT_ENABLED,
    Reason.WORKFLOW_NOT_ALLOWED: WORKFLOW_NOT_ENABLED,
}


def wait_text(seconds: int) -> str:
    """Render a retry delay ("3 minutes", "2 hours")."""
    minutes = max(1, ceil(seconds / 60))
    if minutes >= 120:
        return f"{ceil(minutes / 60)} hours"
    return "1 minute" if minutes == 1 else f"{minutes} minutes"


def rate_limited(seconds: int) -> str:
    """The Ask rate-limit text with the asker's own wait."""
    return f"You're asking a lot; try again in {wait_text(seconds)}."


def question_too_long(limit: int) -> str:
    """The over-length text names the configured limit only."""
    return f"That question is too long. Keep it under {limit} characters."


def tool_red(label: str, reason: Reason) -> str:
    """One red tool for a refusal: access or health, with its display name."""
    if reason in _ACCESS:
        return f"{label} isn't open to everyone in Slack. Ask a Weaves admin."
    word = _HEALTH_WORDS.get(reason, "it's unavailable")
    return f"I can't check {label} right now ({word}); nothing was looked up."


def skipped_note(items: list[tuple[str, Reason]]) -> str:
    """Partial mode's closing line naming what was skipped and why."""
    parts = []
    for label, reason in items:
        if reason in _ACCESS:
            parts.append(f"{label} (not open here)")
        else:
            parts.append(f"{label} ({_HEALTH_WORDS.get(reason, 'unavailable')})")
    return "Skipped: " + ", ".join(parts)


def fixed(reason: Reason) -> str | None:
    """The fixed text of a reason (``None``: drop silently or not a fixed text)."""
    return _FIXED.get(reason)
