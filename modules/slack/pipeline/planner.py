"""The Ask planner: one tool-less model call that picks sources, never data.

Input is the question (marked untrusted, capped) and the presented tools (id,
name, one-line description); output is strict JSON ``{"needs": [...],
"general_ok": bool}``. The planner is a router, not a security boundary: it can
only name presented ids, and authorization is decided afterwards by the engine.
The call runs on the organization's own key and is metered in the usage ledger
under ``{run key}:plan``; a call that misses its deadline still settles its own
row when it finishes.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass

from clients.model_providers.gateway import ModelGateway
from contracts.v1 import (
    ModelMessage,
    ModelProfileBinding,
    ModelRequest,
    ModelResponse,
)
from modules.slack.model_status import is_connectivity_failure, provider_readiness
from modules.slack.pipeline.deadline import DeadlineExceeded, run_with_deadline
from modules.slack.pipeline.models import (
    MAX_PLANNED_TOOLS,
    DiscoveredTool,
    PlannerOutcome,
    PlannerStatus,
    PlannerTarget,
)
from modules.slack.pipeline.planner_prompt import (
    MAX_QUESTION_CHARS,
    SYSTEM_PROMPT,
    build_messages,
)
from modules.usage.service import UsageCall, UsageLimitExceeded, UsageService
from shared.errors import ModelGatewayError
from shared.metrics import SLACK_PLANNER, SLACK_PLANNER_SECONDS

__all__ = [
    "MAX_QUESTION_CHARS",
    "SYSTEM_PROMPT",
    "AskPlanner",
    "ParsedPlan",
    "build_messages",
    "parse_plan",
]

logger = logging.getLogger("product.slack.planner")

PLANNER_TIMEOUT_SECONDS = 8.0
PLANNER_MAX_OUTPUT_TOKENS = 512
_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


@dataclass(frozen=True)
class ParsedPlan:
    """A validated plan, or why it was refused."""

    status: PlannerStatus
    needs: tuple[str, ...] = ()
    general_ok: bool = False


def parse_plan(content: str, presented: frozenset[str]) -> ParsedPlan:
    """Strictly validate the planner's JSON against the presented ids."""
    text = content.strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1)
    try:
        value = json.loads(text)
    except ValueError:
        return ParsedPlan(PlannerStatus.INVALID)
    if not isinstance(value, dict) or set(value) != {"needs", "general_ok"}:
        return ParsedPlan(PlannerStatus.INVALID)
    needs, general_ok = value["needs"], value["general_ok"]
    if not isinstance(general_ok, bool) or not isinstance(needs, list):
        return ParsedPlan(PlannerStatus.INVALID)
    if len(needs) > MAX_PLANNED_TOOLS or not all(isinstance(n, str) for n in needs):
        return ParsedPlan(PlannerStatus.INVALID)
    if len(set(needs)) != len(needs):
        return ParsedPlan(PlannerStatus.INVALID)
    if any(item not in presented for item in needs):
        return ParsedPlan(PlannerStatus.UNPRESENTED)
    if not needs and not general_ok:
        return ParsedPlan(PlannerStatus.INVALID)  # no source and no general answer
    return ParsedPlan(PlannerStatus.VALID, tuple(needs), general_ok)


class AskPlanner:
    """Run and meter the planner call; every failure is a status, never a raise."""

    def __init__(
        self,
        gateway: ModelGateway,
        usage: UsageService,
        *,
        timeout_seconds: float = PLANNER_TIMEOUT_SECONDS,
    ) -> None:
        """Bind the model gateway, the usage ledger service and the deadline."""
        self._gateway = gateway
        self._usage = usage
        self._timeout = timeout_seconds

    def plan(
        self,
        question: str,
        tools: tuple[DiscoveredTool, ...],
        target: PlannerTarget | None,
        *,
        org_id: str,
        usage_key: str,
        request_id: str,
    ) -> PlannerOutcome:
        """Ask the planner which of ``tools`` the question needs."""
        profile_id = target.profile.id if target is not None else None
        started = time.monotonic()
        if (
            target is None
            or not provider_readiness(target.profile, target.provider).connected
        ):
            return self._finish(PlannerStatus.NO_MODEL, started, profile_id)
        presented = frozenset(tool.tool_id for tool in tools)
        messages = build_messages(question, tools)
        try:
            response = run_with_deadline(
                lambda: self._metered_call(
                    messages, target, org_id, usage_key, request_id
                ),
                self._timeout,
            )
        except DeadlineExceeded:
            return self._finish(PlannerStatus.TIMEOUT, started, profile_id)
        except ModelGatewayError as error:
            lost = is_connectivity_failure(error.code, error.http_status, error.message)
            status = PlannerStatus.NO_MODEL if lost else PlannerStatus.ERROR
            return self._finish(status, started, profile_id)
        except (UsageLimitExceeded, ValueError):
            return self._finish(PlannerStatus.ERROR, started, profile_id)
        except Exception:  # a planner problem must fail closed, never crash a turn
            logger.exception("planner call failed")
            return self._finish(PlannerStatus.ERROR, started, profile_id)
        parsed = parse_plan(response.content, presented)
        return self._finish(
            parsed.status,
            started,
            profile_id,
            needs=parsed.needs,
            general_ok=parsed.general_ok,
            units=response.usage.input_tokens + response.usage.output_tokens,
        )

    @staticmethod
    def _finish(
        status: PlannerStatus,
        started: float,
        profile_id: str | None,
        *,
        needs: tuple[str, ...] = (),
        general_ok: bool = False,
        units: int | None = None,
    ) -> PlannerOutcome:
        SLACK_PLANNER.inc(status=status.value)
        SLACK_PLANNER_SECONDS.observe(time.monotonic() - started)
        return PlannerOutcome(status, needs, general_ok, units, profile_id)

    def _metered_call(
        self,
        messages: tuple[ModelMessage, ModelMessage],
        target: PlannerTarget,
        org_id: str,
        usage_key: str,
        request_id: str,
    ) -> ModelResponse:
        """Reserve, call and settle: the ledger resolves however the call ends."""
        profile = target.profile
        request = ModelRequest(
            messages=messages,
            temperature=0.0,
            max_output_tokens=min(profile.max_output_tokens, PLANNER_MAX_OUTPUT_TOKENS),
            request_id=request_id,
        )
        call = UsageCall(
            org_id=org_id,
            workspace_id=target.workspace_id,
            agent_run_id=usage_key,
            run_id=usage_key,
            agent_id=target.agent_id,
            provider_id=target.provider.id,
            model_id=profile.model,
            input_rate_per_million=profile.input_cost_per_million_tokens_usd,
            output_rate_per_million=profile.output_cost_per_million_tokens_usd,
        )
        self._usage.begin(call, request)
        binding = ModelProfileBinding(
            provider=target.provider, profile=profile, access_mode=target.access_mode
        )
        try:
            response = self._gateway.complete(request, binding)
        except ModelGatewayError as exc:
            self._usage.fail(
                call, exc.code, http_status=exc.http_status, usage=exc.usage
            )
            raise
        self._usage.settle(
            call,
            response.usage.input_tokens,
            response.usage.output_tokens,
            "ok",
            provider_id=response.provider_id,
            model_id=response.model_id,
        )
        return response
