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
