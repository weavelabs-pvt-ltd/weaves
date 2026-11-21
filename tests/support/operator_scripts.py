"""Load and run the operator-only VM scripts against an injected runtime."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parents[2] / "infra/gcp/singlevm"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

bootstrap_account_owner = importlib.import_module("bootstrap_account_owner")
issue_account_session = importlib.import_module("issue_account_session")
backfill_schedule_timezones = importlib.import_module("backfill_schedule_timezones")


def run_script(
    module: Any, capsys: Any, runtime: Any, *args: str
) -> tuple[int, str, list[dict[str, Any]]]:
    """Run a script's ``run`` and return (exit status, stdout, stderr log lines)."""
    capsys.readouterr()
    status = module.run(list(args), runtime)
    captured = capsys.readouterr()
    logs = [json.loads(line) for line in captured.err.splitlines() if line.strip()]
    return status, captured.out, logs
