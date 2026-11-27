"""Values of the Slack request pipeline: its request, its result, its tools.

``run_pipeline(PipelineRequest) -> PipelineResult`` is the one interface the
dispatcher and the worker guard use. Nothing here carries message text except
``PipelineRequest.question``, which is excluded from ``repr`` and is never
copied into a result, a log line, a metric or the audit record.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from contracts.v1 import ModelAccessMode, ModelProfile, ModelProvider
from modules.slack.access.decision_audit import DecisionContext, PlannerRecord
from modules.slack.access.models import (
    AccessRequest,
    Decision,
    Outcome,
    ToolHealth,
)

# The capability a PR review needs. It does not exist until the PR review slice
# (S6) ships; until then the placeholder is discovered as unavailable.
PR_REVIEW_CAPABILITY = "github.pr.review_context"
MAX_PLANNED_TOOLS = 3
MAX_PRESENTED_TOOLS = 40


class PlannerStatus(StrEnum):
    """How the Ask planner ended (the audit ``planner.status`` values)."""

    VALID = "valid"
    INVALID = "invalid"
    UNPRESENTED = "unpresented"
    TIMEOUT = "timeout"
    ERROR = "error"
    NO_MODEL = "no_model"  # no connected model, or it was unreachable
    SKIPPED = "skipped"

    @property
    def no_model(self) -> bool:
        """The planner could not reach a connected model (no model, or it timed out)."""
        return self in (PlannerStatus.NO_MODEL, PlannerStatus.TIMEOUT)

    @property
    def failed(self) -> bool:
        """Whether the planner produced no usable plan."""
        return self not in (PlannerStatus.VALID, PlannerStatus.SKIPPED)


@dataclass(frozen=True)
class DiscoveredTool:
    """One read capability of the organization, with its status right now.

    ``health`` is ``None`` when the snapshot is missing, stale or errored: the
    pipeline then probes live, but only if the plan needs this tool.
    """

    tool_id: str
    installation_id: str
    org_id: str
    label: str
    name: str
    description: str
    plugin_type: str
    health: ToolHealth | None
    # The snapshot was errored (not merely old); a failed re-probe stays ERROR.
    was_error: bool = False
    projects: frozenset[str] = frozenset()
    repositories: frozenset[str] = frozenset()

    @property
    def presentable(self) -> bool:
        """Offered to the planner: healthy or worth a live re-probe."""
        return self.health is None or self.health is ToolHealth.HEALTHY


@dataclass(frozen=True)
class PlannerTarget:
    """The org's model profile and provider the planner call runs on."""

    profile: ModelProfile
    provider: ModelProvider
    access_mode: ModelAccessMode | None
    workspace_id: str
    agent_id: str


@dataclass(frozen=True)
class PlannerOutcome:
    """What the planner call produced; ``needs`` only when ``VALID``."""

    status: PlannerStatus
    needs: tuple[str, ...] = ()
    general_ok: bool = False
    usage_units: int | None = None
    profile_id: str | None = None

    def record(self) -> PlannerRecord:
        """The audit view (status and token units, nothing else)."""
        return PlannerRecord(
            used=self.status is not PlannerStatus.SKIPPED,
            status=self.status.value,
            profile_id=self.profile_id,
            usage_units=self.usage_units,
        )


@dataclass(frozen=True)
class PipelineRequest:
    """Everything one request needs; the engine inputs arrive pre-resolved.

    ``access`` carries the asker's standing, the settings, the platform and org
    state and (for Execute) the repo or workflow target, with no tools: the
    pipeline fills the needed tools in. ``usage_key`` is the planner's ledger
    key (``{run key}:plan``).
    """

    access: AccessRequest
    context: DecisionContext
    usage_key: str
    question: str = field(default="", repr=False)
    planner_target: PlannerTarget | None = None
    workspace_id: str = ""
    workflow_capability_ids: tuple[str, ...] = ()
    # ``create_task``: the capabilities the task's pinned agent version lists. The
    # task may use those of them that are read tools and green, never more.
    agent_capability_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class PipelineResult:
    """The pipeline's answer for the dispatcher, the guard and the reply step.

    ``proceed`` means the answering run may start, and then only with
    ``capability_ids`` (needed AND green; empty means model-only). When it is
    false nothing was looked up and the answer model must not be called:
    ``message`` is the fixed, list-free text to show.
    """

    decision: Decision
    outcome: Outcome
    proceed: bool
    capability_ids: tuple[str, ...]
    tools_considered: tuple[str, ...]
    tools_needed: tuple[str, ...]
    planner: PlannerOutcome
    message: str | None = None
    skipped_note: str | None = None
    answer_ephemeral: bool = False
    request_id: str = ""
    audit_event_id: str | None = None

    @property
    def short_circuit(self) -> bool:
        """True when the answer model and every connector are skipped."""
        return not self.proceed
