"""What the pull request reader reported, read back from its tool invocation.

The capability returns counts (``stats``) next to its documents and the agent
runtime keeps them on the ``ToolInvocation``; that record is the one place the
reply tick learns the pull request's title and file counts, both while the review
is still being written (the first message names the pull request and its files)
and afterwards (the receipt and the footer). A failed read keeps its fixed error
code there too. Nothing here reads message text.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from application.ports.repository import Repository
from contracts.v1 import InvocationStatus, TaskTurn, ToolInvocation, WorkflowRun
from modules.integrations.github_review_spec import PR_REVIEW_CAPABILITY_ID

SKIP_KINDS = ("generated", "size", "no_patch", "sensitive")


@dataclass(frozen=True)
class ReviewStats:
    """Counts the reader reports; ``title`` is untrusted text."""

    repository: str
    number: int
    title: str = field(repr=False)
    files_total: int
    files_reviewed: int
    skipped: Mapping[str, int]

    @property
    def ref(self) -> str:
        """``owner/repo#number``."""
        return f"{self.repository}#{self.number}"

    @classmethod
    def from_summary(cls, summary: Mapping[str, Any] | None) -> ReviewStats | None:
        """Parse the invocation's ``output_summary`` (``None`` if it is not ours)."""
        raw = summary.get("stats") if summary else None
        if not isinstance(raw, Mapping):
            return None
        repository, title = raw.get("repository"), raw.get("title", "")
        number, total = raw.get("number"), raw.get("files_total")
        reviewed, skipped = raw.get("files_reviewed"), raw.get("skipped", {})
        if (
            not isinstance(repository, str)
            or not isinstance(title, str)
            or not all(_count(v) for v in (number, total, reviewed))
            or not isinstance(skipped, Mapping)
        ):
            return None
        return cls(
            repository,
            int(number),  # type: ignore[arg-type]
            title,
            int(total),  # type: ignore[arg-type]
            int(reviewed),  # type: ignore[arg-type]
            {k: v for k, v in skipped.items() if k in SKIP_KINDS and _count(v)},
        )


def _count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


class ReviewProgress:
    """Find a review turn's reader invocation and read its stats or error."""

    def __init__(
        self,
        *,
        runs: Repository[WorkflowRun],
        invocations: Repository[ToolInvocation],
    ) -> None:
        """Bind the (tenant-scoped) run and invocation records."""
        self._runs = runs
        self._invocations = invocations

    def invocation(self, turn: TaskTurn) -> ToolInvocation | None:
        """The turn's pull request read, once the runtime has recorded it."""
        if not turn.run_idempotency_key:
            return None
        run = self._runs.find_by_idempotency_key(
            turn.run_idempotency_key,
            turn.requested_by_principal_id,
            turn.org_id,
            turn.workspace_id,
        )
        if run is None:
            return None
        for item in self._invocations.list_by_field("run_id", run.run_id):
            if (
                item.org_id == turn.org_id
                and item.capability_id == PR_REVIEW_CAPABILITY_ID
            ):
                return item
        return None

    def stats(self, turn: TaskTurn) -> ReviewStats | None:
        """The counts of a successful read."""
        found = self.invocation(turn)
        if found is None or found.status is not InvocationStatus.SUCCEEDED:
            return None
        return ReviewStats.from_summary(found.output_summary)

    def error_code(self, turn: TaskTurn) -> str | None:
        """The fixed code of a failed read (``plugin.pr_not_found`` and friends)."""
        found = self.invocation(turn)
        if found is None or found.status is not InvocationStatus.FAILED:
            return None
        return found.error.code if found.error is not None else None
