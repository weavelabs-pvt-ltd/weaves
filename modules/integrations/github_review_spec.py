"""The descriptor of ``github.pr.review_context`` (read-only pull request snapshot)."""

from typing import Any

from contracts.v1 import CapabilityKind, CapabilitySpec, RiskLevel

PR_REVIEW_CAPABILITY_ID = "github.pr.review_context"

_DOCUMENT: dict[str, Any] = {
    "type": "object",
    "properties": {
        "source_id": {"type": "string", "maxLength": 256},
        "title": {"type": "string", "maxLength": 160},
        "text": {"type": "string", "maxLength": 2000},
    },
    "required": ["source_id", "title", "text"],
    "additionalProperties": False,
}
_COUNT: dict[str, Any] = {"type": "integer", "minimum": 0, "maximum": 10_000_000}
_STATS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "repository": {"type": "string", "maxLength": 200},
        "number": _COUNT,
        "title": {"type": "string", "maxLength": 200},
        "files_total": _COUNT,
        "files_reviewed": _COUNT,
        "files_skipped": _COUNT,
        "skipped": {
            "type": "object",
            "properties": {
                kind: _COUNT for kind in ("generated", "size", "no_patch", "sensitive")
            },
            "additionalProperties": False,
        },
        "truncated": _COUNT,
    },
    "required": ["repository", "number", "files_total", "files_reviewed"],
    "additionalProperties": False,
}


def github_pr_review_capability() -> CapabilitySpec:
    """Input is the task text (the link is parsed from it); output is documents."""
    return CapabilitySpec(
        capability_id=PR_REVIEW_CAPABILITY_ID,
        name="Read a GitHub pull request for review",
        description=(
            "Read one pull request (its description and changed files with "
            "patches, size-capped) from a connected repository. Read-only."
        ),
        kind=CapabilityKind.CONTEXT_READ,
        risk_level=RiskLevel.LOW,
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 500}
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {
                "documents": {"type": "array", "maxItems": 26, "items": _DOCUMENT},
                "stats": _STATS,
            },
            "required": ["documents"],
            "additionalProperties": False,
        },
        timeout_seconds=30,
    )
