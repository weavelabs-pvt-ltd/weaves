"""Every text the bot says in the Ask flow (the UX contract of design section 2).

Fixed strings or built only from ids, counts and durations; nothing here takes a
user's words or a connector's data, except ``evidence_line`` which takes
document *titles* (they are sanitized and scrubbed with the rest of the message
before they leave). Standard Slack emoji only. The S3 texts for blocked asks
(``modules.slack.access.messages``) are reused as they are; this module adds
the scenes the access engine does not own.
"""

from __future__ import annotations

from collections.abc import Sequence

# Scene H: small exits (no task, no model).
HELP = (
    "Tag me with a question and I'll look it up in your connected sources. "
    "For example:\n"
    "• @Weaves what is the status of WEAVES-12?\n"
    "• @Weaves what changed in the checkout service this week?\n"
    "• @Weaves status"
)
GREETING = "Hi! Tag me with a question in a thread and I'll look it up."
THANKS = "You're welcome."

# Execute commands that arrive in later slices: recognized, never executed.
COMING_SOON = {
    "review": "Pull request reviews from Slack are coming soon.",
    "run": "Running workflows from Slack is coming soon.",
}

# Scene F: missing detail (a re-mention is needed to answer it).
MISSING_PR = (
    "Reply with @Weaves and the PR link, for example "
    "@Weaves review https://github.com/acme/api/pull/42"
)
MISSING_WORKFLOW = (
    "Tell me which workflow to run, for example @Weaves run weekly-report"
)

# Scene E: an Execute command whose rule is not satisfied.
NOT_SET_UP = {
    "task": "I'm not set up to create tasks here. Ask a Weaves admin.",
    "review": "I'm not set up to review pull requests here. Ask a Weaves admin.",
    "run": "I'm not set up to run workflows here. Ask a Weaves admin.",
}

# Scene C: creating a task (design F2); the link buttons carry the labels below.
TASK_DEFAULT_TITLE = "Slack task"
TASK_NO_TEXT = "The task finished without a written result."
TASK_FULL_RESULT = "Full result and sources in Weaves."
MISSING_TASK_TEXT = (
    "Tell me what the task is, for example @Weaves task: prepare the weekly KPI summary"
)
NO_TASK_AGENT = "No agent is set up for Slack tasks. Ask a Weaves admin."
VIEW_IN_WEAVES = "View in Weaves"
VIEW_TASK = "View task"
OPEN_RESULT = "Open result"
TASK_STOPPED = ":octagonal_sign: Stopped."


def task_created(title: str, agent: str) -> str:
    """The first message, edited later into the receipt."""
    return (
        f":white_check_mark: Task created: {title} · Agent: {agent} · Running · "
        "I'll post the result here"
    )


def task_done(title: str, agent: str, elapsed: str) -> str:
    """The receipt the first message becomes when the task finished."""
    return f":white_check_mark: Task done: {title} · {agent} · {elapsed}"


def task_failed(title: str, ref: str) -> str:
    """The first message when the task could not finish (no error text)."""
    return f":warning: I couldn't finish this task: {title} (ref {ref})."


def already_tracked(title: str) -> str:
    """A repeat ``task:`` in a thread that already has its task."""
    return f"Already tracked in this thread: {title}"


# Scene I: system states.
NO_MODEL = "No AI model is connected. Ask a Weaves admin."
BUSY = (
    "I'm still working on your last question in this thread. "
    "Ask again when I've replied."
)
NO_ACTIVITY = "Nothing is running in this thread."

# Scene G: waiting and failing.
QUEUED = ":hourglass_flowing_sand: Queued. I'll start as soon as a slot is free."
TAKING_LONGER = (
    ":hourglass_flowing_sand: This is taking longer than expected. "
    "I'll keep going and post here."
)
CHECKING = ":mag: Checking sources…"
WRITING = ":pencil2: Writing the answer…"
RECEIPT = ":white_check_mark: Done in {elapsed}"
PRIVATE_RECEIPT = ":lock: I've replied to you privately."
UNDELIVERED = ":warning: I couldn't post the answer here (ref {ref})."


# Stop and Try again (design 3.12): buttons and the ``@Weaves stop|retry`` fallback.
STOPPED = ":octagonal_sign: Stopped."
RETRYING = ":arrows_counterclockwise: Retrying…"
ALREADY_STOPPED = "Already stopped."
ALREADY_RETRYING = "Already retrying."
NO_LONGER_AVAILABLE = "That's no longer available."
ONLY_REQUESTER_STOP = "Only the person who asked, or an admin, can stop this."
ONLY_REQUESTER_RETRY = "Only the person who asked, or an admin, can retry this."
RETRY_LIMIT = (
    "I've already retried this 3 times. Mention me with your question to ask again."
)
NOTHING_TO_RETRY = "There's nothing to retry in this thread."


def failure(ref: str) -> str:
    """The failed-turn message with a short reference for support."""
    return f":warning: I couldn't finish this (ref {ref})."


def still_working(minutes: int) -> str:
    """Scene G after two minutes."""
    return f":hourglass_flowing_sand: Still working ({minutes} min)…"


def checking(label: str) -> str:
    """A real pipeline step: a connector is being read."""
    return f":mag: Checking {label}…"


def refusal(text: str, outcome: str) -> str:
    """Prefix a refusal with its standard emoji (the lock for access and rules)."""
    if outcome in ("refused_access", "blocked_rule"):
        return f":lock: {text}"
    if outcome == "rate_limited":
        return f":hourglass_flowing_sand: {text}"
    return text


def duration(seconds: float) -> str:
    """``41s`` or ``2m 05s``."""
    whole = max(0, round(seconds))
    if whole < 60:
        return f"{whole}s"
    return f"{whole // 60}m {whole % 60:02d}s"


def evidence_line(titles: Sequence[str], source_count: int) -> str:
    """The ``From …`` line (document titles only) and the sources count."""
    if source_count == 0:
        return "From general knowledge"
    shown = [t.strip()[:60] for t in titles if t.strip()][:3]
    more = max(0, source_count - len(shown))
    names = "; ".join(shown) if shown else "connected sources"
    extra = f" (+{more} more)" if more and shown else ""
    return f"From {names}{extra}"


def sources_count(count: int) -> str:
    """``3 sources`` (``1 source``)."""
    return f"{count} source" + ("" if count == 1 else "s")


def status_report(label: str, seconds: float) -> str:
    """The private ``@Weaves status`` text for a running request."""
    return f"Working on your last question: {label} ({duration(seconds)} so far)."
