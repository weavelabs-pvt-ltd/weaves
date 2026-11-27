"""The words of a PR review in Slack (scene D): fixed text plus sanitized data.

The pull request title, the model's summary, finding texts, file names and the
question are all untrusted: each goes through ``plain`` (control and bidi
characters removed, one line, converted and neutralized by the mrkdwn sanitizer
so ``<!channel>``, ``<@U..>`` and links lose their power) before it is placed in
a template of ours. Standard Slack emoji only. Nothing here names who asked.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from urllib.parse import urlsplit

from clients.integrations.github_pr_files import clean_text
from modules.slack import copy
from modules.slack.review.result import Finding, Review
from modules.slack.review.stats import ReviewStats
from modules.slack.text.mrkdwn import MAX_MESSAGE_CHARS, escape, fit, to_mrkdwn

DEFAULT_FRONTEND = "https://app.weave-studio.in"
UNREADABLE = (
    "I can't read that PR. Check the link, or ask a Weaves admin to allow this "
    "repository."
)
NO_REVIEW = "I couldn't produce a review. Try again."
GITHUB_BUSY = "GitHub is limiting requests right now. Try again in a few minutes."
NOTHING_POSTED = "Nothing was posted on GitHub"
MISSING_PR = copy.MISSING_PR
UNREADABLE_CODES = frozenset(
    {
        "plugin.pr_not_found",
        "plugin.pr_access_denied",
        "plugin.pr_repo_not_covered",
        "plugin.pr_url_invalid",
    }
)
RISK = {
    "low": (":large_green_circle:", "Low"),
    "medium": (":large_orange_circle:", "Medium"),
    "high": (":red_circle:", "High"),
}
_RANK = {"high": 0, "medium": 1, "low": 2}
_SKIP_LABELS = {
    "generated": "generated and lock files",
    "size": "files over the size limit",
    "no_patch": "files without a readable diff",
    "sensitive": "sensitive files",
}
_PATH = re.compile(r"[`<>|*\s]")


def plain(text: str, limit: int) -> str:
    """Untrusted text as one safe mrkdwn line of at most ``limit`` raw characters."""
    line = " ".join(clean_text(text, limit * 2).split())[:limit].strip()
    return " ".join(to_mrkdwn(line).split())


def file_label(path: str) -> str:
    """A file path in code style, with anything that could break the span removed."""
    return f"`{escape(_PATH.sub('_', clean_text(path, 200)))[:120]}`"


def frontend_origin() -> str:
    """The web app's origin (for the "Open in Weaves" link), or empty if unusable."""
    value = (os.environ.get("WEAVES_PUBLIC_FRONTEND_URL") or DEFAULT_FRONTEND).rstrip(
        "/"
    )
    parts = urlsplit(value)
    if (
        parts.scheme in ("http", "https")
        and parts.hostname
        and not parts.path.strip("/")
    ):
        return f"{parts.scheme}://{parts.netloc}"
    return ""


def task_link(task_id: str) -> str:
    """``<url|Open in Weaves>`` (empty when the origin is unusable)."""
    origin = frontend_origin()
    if not origin or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", task_id):
        return ""
    return f"<{origin}/#tasks/{task_id}|Open in Weaves>"


def _files(count: int) -> str:
    return f"{count} file" + ("" if count == 1 else "s")


def working_start(ref: str) -> str:
    """The first message before the reader has answered."""
    return f":mag: Reviewing {escape(ref)}…"


def working(stats: ReviewStats) -> str:
    """The first message once the pull request and its file count are known."""
    title = plain(stats.title, 120)
    return (
        f':mag: Reviewing {escape(stats.ref)}, "{title}" · {_files(stats.files_total)}'
    )


def receipt(stats: ReviewStats, elapsed_seconds: float) -> str:
    """The first message, edited into a receipt when the review is posted."""
    return (
        f':white_check_mark: Reviewed {escape(stats.ref)}, "{plain(stats.title, 120)}"'
        f" · {stats.files_reviewed} of {stats.files_total} files · "
        f"{copy.duration(elapsed_seconds)}"
    )


def _skipped(stats: ReviewStats) -> str:
    kinds = [kind for kind in _SKIP_LABELS if stats.skipped.get(kind)]
    if not kinds:
        return ""
    labels = [_SKIP_LABELS[kind] for kind in kinds]
    joined = (
        labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " and " + labels[-1]
    )
    return f" (skipped {joined})"


def footer(stats: ReviewStats, task_id: str) -> str:
    """``Reviewed N of M files · Nothing was posted on GitHub · Open in Weaves``."""
    parts = [
        f"Reviewed {stats.files_reviewed} of {stats.files_total} files"
        f"{_skipped(stats)}",
        NOTHING_POSTED,
    ]
    link = task_link(task_id)
    if link:
        parts.append(link)
    return "_" + " · ".join(parts) + "_"


def _findings(findings: Sequence[Finding]) -> list[str]:
    if not findings:
        return ["No issues found in the files I read."]
    ordered = sorted(findings, key=lambda item: _RANK[item.severity])
    return [f"• {file_label(f.file)}: {plain(f.issue, 300)}" for f in ordered]


def review_message(review: Review, stats: ReviewStats, task_id: str) -> str:
    """Message two: summary, risk, findings, one question, and the footer."""
    icon, word = RISK[review.risk]
    body = "\n".join(
        [
            f"*Review of {escape(stats.ref)}*",
            plain(review.summary, 700),
            "",
            f"{icon} *{word} risk*",
            "",
            "*Findings*",
            *_findings(review.findings),
            "",
            f"*Question for the author:* {plain(review.question, 300)}",
        ]
    )
    foot = footer(stats, task_id)
    return f"{fit(body, MAX_MESSAGE_CHARS - len(foot) - 2).text}\n\n{foot}"
