"""The request-time pipeline: discover, plan, check, respond (design 3.5.1).

``SlackPipeline.run_pipeline(request) -> PipelineResult`` runs steps 1 to 9 for
one Slack request, in this order and never skipping one:

1. gates: the S3 engine without tools (platform, org, installation, pause,
   limits, tier rule, Execute pre-flight). A refusal here costs no planner call
   and touches no connector;
2. DISCOVER: the organization's read tools and their cached health;
3. PLAN: Execute uses a fixed grammar and no model; Ask makes one tool-less
   planner call (skipped when no tool can be offered) and unions regex hints;
   a failed planner fails closed when any tool was offered;
4. CHECK: each needed tool's health (live re-probe only if stale) and access,
   decided by the engine, never by the planner;
5. RESPOND: strict or partial per the settings; the result names the only
   capability ids the answering run may use.

Exactly one ``slack.decision`` record is written, with ids and codes only.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from modules.slack.access import messages
from modules.slack.access.decision_audit import DecisionRecorder
from modules.slack.access.engine import evaluate
from modules.slack.access.models import (
    ACCESS_REASONS,
    AccessRequest,
    Action,
    AskUsage,
    Decision,
    NeededTool,
    Outcome,
    Reason,
    ToolHealth,
)
from modules.slack.pipeline.discovery import ToolDiscovery
from modules.slack.pipeline.hints import hinted_tool_ids
from modules.slack.pipeline.models import (
    MAX_PRESENTED_TOOLS,
    PR_REVIEW_CAPABILITY,
    DiscoveredTool,
    PipelineRequest,
    PipelineResult,
    PlannerOutcome,
    PlannerStatus,
)
from modules.slack.pipeline.planner import AskPlanner
from shared.metrics import SLACK_ASK, SLACK_EXECUTE, SLACK_TOOL_CHECKS

_SKIPPED = PlannerOutcome(PlannerStatus.SKIPPED)
_FAMILY_LABELS = {"github": "GitHub", "jira": "Jira", "slack": "Slack"}


class _Once:
    """Evaluate the audit-derived Ask usage once per request, not per engine pass."""

    def __init__(self, load: Callable[[], AskUsage]) -> None:
        self._load = load
        self._value: AskUsage | None = None

    def __call__(self) -> AskUsage:
        if self._value is None:
            self._value = self._load()
        return self._value


class SlackPipeline:
    """Discover, plan, check and respond for one request at a time."""

    def __init__(
        self,
        *,
        discovery: ToolDiscovery,
        planner: AskPlanner,
        recorder: DecisionRecorder,
    ) -> None:
        """Bind discovery, the planner and the decision recorder."""
        self._discovery = discovery
        self._planner = planner
        self._recorder = recorder

    def run_pipeline(self, request: PipelineRequest) -> PipelineResult:
        """Run the pipeline once and record its single decision."""
        access = request.access
        load = access.ask_usage
        base = replace(
            access,
            ask_usage=_Once(load) if load is not None else None,
            question_chars=len(request.question) if access.action.is_ask else 0,
            tools=(),
        )
        gate = evaluate(base)
        if not gate.allowed:
            return self._respond(request, gate, (), (), _SKIPPED)
        tools = self._discovery.discover(
            request.context.org_id, request.workspace_id, base.now
        )
        considered = tuple(tool.tool_id for tool in tools)
        ids, planner, failed = self._plan(request, tools)
        if failed:
            return self._respond(
                request, _planner_failed(base, gate), considered, (), planner
            )
        needed = self._check_inputs(base, ids, tools)
        decision = evaluate(replace(base, tools=needed))
        return self._respond(request, decision, considered, ids, planner)

    # -- PLAN ---------------------------------------------------------------

    def _plan(
        self, request: PipelineRequest, tools: tuple[DiscoveredTool, ...]
    ) -> tuple[tuple[str, ...], PlannerOutcome, bool]:
        """The needed tool ids, the planner outcome and whether to fail closed."""
        action = request.access.action
        if action.is_execute:
            return _execute_needs(request, tools), _SKIPPED, False
        presented = tuple(t for t in tools if t.presentable)[:MAX_PRESENTED_TOOLS]
        hints = hinted_tool_ids(request.question, tools)
        if not presented:
            return hints, _SKIPPED, False
        outcome = self._planner.plan(
            request.question,
            presented,
            request.planner_target,
            org_id=request.context.org_id,
            usage_key=request.usage_key,
            request_id=request.context.request_id,
        )
        if outcome.status.failed:
            return (), outcome, True
        return tuple(dict.fromkeys((*outcome.needs, *hints))), outcome, False

    # -- CHECK --------------------------------------------------------------

    def _check_inputs(
        self,
        base: AccessRequest,
        ids: tuple[str, ...],
        tools: tuple[DiscoveredTool, ...],
    ) -> tuple[NeededTool, ...]:
        """Needed tools with settled health: probe only what access already allows.

        A tool the asker may not use is red by access whatever its health, so it is
        never probed; a tool with a fresh snapshot is never probed either.
        """
        by_id = {tool.tool_id: tool for tool in tools}
        found = [by_id.get(tool_id) for tool_id in ids]
        first = tuple(
            _needed(tool_id, tool, tool.health if tool else None)
            for tool_id, tool in zip(ids, found, strict=True)
        )
        if all(tool is None or tool.health is not None for tool in found):
            return first
        # Optimistic pass: which tools does access refuse before health matters?
        refused = {
            r.tool_id
            for r in evaluate(replace(base, tools=first)).tool_results
            if r.reason in ACCESS_REASONS
        }
        probed: dict[str, ToolHealth] = {}
        settled: list[NeededTool] = []
        for tool_id, tool in zip(ids, found, strict=True):
            if tool is None or tool.health is not None:
                settled.append(_needed(tool_id, tool, tool.health if tool else None))
            elif tool_id in refused:
                settled.append(_needed(tool_id, tool, ToolHealth.UNAVAILABLE))
            else:
                settled.append(
                    _needed(tool_id, tool, self._discovery.settle(tool, probed))
                )
        return tuple(settled)

    # -- RESPOND ------------------------------------------------------------

    def _respond(
        self,
        request: PipelineRequest,
        decision: Decision,
        considered: tuple[str, ...],
        needed: tuple[str, ...],
        planner: PlannerOutcome,
    ) -> PipelineResult:
        event = self._recorder.record(
            decision,
            request.context,
            request.access.now,
            planner=planner.record(),
            considered=considered,
        )
        _count(decision)
        return PipelineResult(
            decision=decision,
            outcome=decision.outcome,
            proceed=decision.allowed,
            capability_ids=decision.green_tools if decision.allowed else (),
            tools_considered=considered,
            tools_needed=tuple(r.tool_id for r in decision.tool_results) or needed,
            planner=planner,
            message=decision.message,
            skipped_note=decision.skipped_note,
            answer_ephemeral=decision.answer_ephemeral,
            request_id=request.context.request_id,
            audit_event_id=event.audit_event_id,
        )


def _needed(
    tool_id: str, tool: DiscoveredTool | None, health: ToolHealth | None
) -> NeededTool:
    """The engine's view of a needed tool (unknown health reads as healthy)."""
    if tool is None:  # e.g. the PR review capability before S6 ships it
        family = tool_id.split(".", 1)[0]
        label = _FAMILY_LABELS.get(family, family.title())
        return NeededTool(tool_id, "", label, ToolHealth.UNAVAILABLE)
    return NeededTool(
        tool_id, tool.installation_id, tool.label, health or ToolHealth.HEALTHY
    )


def _execute_needs(
    request: PipelineRequest, tools: tuple[DiscoveredTool, ...]
) -> tuple[str, ...]:
    """Execute plans come from a fixed grammar, with no model call.

    A created task needs the read tools its agent lists (write capabilities are
    never run by the worker and are not discovered, so they drop out here).
    """
    action = request.access.action
    if action is Action.CREATE_TASK:
        known = {tool.tool_id for tool in tools}
        return tuple(
            i for i in dict.fromkeys(request.agent_capability_ids) if i in known
        )
    if action is Action.PR_REVIEW:
        return (PR_REVIEW_CAPABILITY,)
    if action is Action.RUN_WORKFLOW:
        return request.workflow_capability_ids
    return ()


def _planner_failed(base: AccessRequest, gate: Decision) -> Decision:
    """Fail closed: no data and no general answer, only a short retry message."""
    return Decision(
        allowed=False,
        outcome=Outcome.PLANNER_FAILED,
        action=base.action,
        mode=gate.mode,
        settings_version=base.settings.version,
        reason=Reason.PLANNER_FAILED,
        message=messages.PLANNER_FAILED,
        checks=(*gate.checks, "planner"),
    )


def _count(decision: Decision) -> None:
    """Metrics by outcome and reason code (never an organization or a user)."""
    outcome = decision.outcome.value
    if decision.action.is_ask:
        SLACK_ASK.inc(outcome=outcome)
    else:
        SLACK_EXECUTE.inc(action=decision.action.value, outcome=outcome)
    for result in decision.tool_results:
        SLACK_TOOL_CHECKS.inc(
            result="green" if result.green else "red",
            reason=result.reason.value if result.reason else "ok",
        )
