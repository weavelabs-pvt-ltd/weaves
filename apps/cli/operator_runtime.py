"""Shared plumbing for operator-only scripts run on the host with owner access.

These helpers are never imported by the HTTP application. They open the runtime
on the database owner's connection (no tenant row policies) and emit one
structured, secret-free log line per operation.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from apps.platform_runtime import LocalPlatformRuntime
from apps.runtime_wiring import open_process_runtime


def open_owner_runtime() -> LocalPlatformRuntime:
    """Open a production-shaped runtime on the owner connection (app_role='')."""
    runtime = open_process_runtime(process="cli", app_role="")
    if runtime.seed_local_defaults:
        runtime.close()
        raise RuntimeError("Operator scripts require WEAVES_SEED_LOCAL_DEFAULTS=false")
    return runtime


def emit(event: str, **fields: Any) -> None:
    """Write one JSON log line to stderr; callers pass identifiers, never secrets."""
    line = {"event": event, **fields}
    sys.stderr.write(json.dumps(line, sort_keys=True, default=str) + "\n")
    sys.stderr.flush()


def record_action(
    runtime: Any,
    *,
    operator: str,
    command: str,
    reason: str,
    target: str = "",
    org_id: str | None = None,
    result: str = "ok",
    details: dict[str, Any] | None = None,
    ticket: str | None = None,
    correlation_id: str | None = None,
) -> bool:
    """Append one ``operator_actions`` row; False (and a log line) if it failed.

    Every mutating operator command calls this once, for success and for refusal.
    A runtime without an audit store (an old test double) is skipped, never an
    error; a real write failure is reported so the caller can exit non-zero.
    """
    audit = getattr(runtime, "operator_audit", None)
    if audit is None:
        return True
    try:
        audit.record(
            operator=operator,
            command=command,
            reason=reason,
            target=target,
            org_id=org_id,
            result=result,
            details=details,
            ticket=ticket,
            correlation_id=correlation_id,
        )
    except Exception as exc:
        emit("operator.audit_failed", command=command, error=type(exc).__name__)
        return False
    return True
