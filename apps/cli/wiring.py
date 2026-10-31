"""Compose dependencies exposed to the agent command-line app."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from apps.platform_runtime import LocalPlatformRuntime
from apps.runtime_wiring import open_process_runtime
from modules.workflows.runtime_result import RuntimeResult


@dataclass(frozen=True)
class CliServices:
    """Expose the agent run use case and process cleanup to the CLI."""

    run_agent: Callable[[str], RuntimeResult]
    close: Callable[[], None]


def build_cli_services() -> CliServices:
    """Build the agent command dependencies and retain client cleanup."""
    runtime: LocalPlatformRuntime = open_process_runtime(process="cli")
    return CliServices(
        run_agent=runtime.agent_execution_service.run_agent,
        close=runtime.close,
    )
