"""Collect read-only production facts for the private-beta gate (run on the VM).

    # on the VM, as root, from /opt/weaves/backend/infra/gcp/singlevm:
    sudo python3 /path/to/gate_collect_production.py > gate-state.json
    # or stream it from your laptop through IAP SSH:
    #   gcloud compute ssh weaves-personal ... --command "sudo python3 -"
    #     < scripts/gate_collect_production.py > gate-state.json

It reads, and changes nothing: an allow-list of non-secret ``runtime.env``
names (the allowlist of emails is reduced to "is it set"), the age of the newest
on-host dump, and ``platform_control.py status`` (a read-only subcommand). It
never prints a secret value. The JSON is the input of
``scripts/private_beta_gate.py --production-state``.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

RUNTIME = Path("/opt/weaves/singlevm/runtime.env")
COMPOSE = Path("/opt/weaves/singlevm/compose.yaml")
BACKUPS = Path("/opt/weaves/backups/singlevm")
OPERATOR_DIR = Path("/opt/weaves/backend/infra/gcp/singlevm")
PLAIN = (
    "WEAVES_LIMITS_MODE",
    "WEAVES_ADMISSIONS_PAUSED",
    "WEAVES_ALLOW_ACCOUNT_ORGANIZATION_CREATION",
    "WEAVES_PUBLIC_REGISTRATION",
    "WEAVES_WORKER_SLOTS",
    "WEAVES_LIMIT_MAX_RUNNING_PER_ORG",
)
PRESENCE_ONLY = (
    "WEAVES_ACCOUNT_ALLOWED_EMAILS",
    "WEAVES_ACCOUNT_ALLOWED_EMAIL_DOMAINS",
)


def read_env(path: Path) -> dict[str, str]:
    """Parse ``KEY=value`` lines; comments and blanks are skipped."""
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        text = line.strip()
        if text and not text.startswith("#") and "=" in text:
            key, _, value = text.partition("=")
            values[key.strip()] = value.strip().strip("'\"")
    return values


def newest_backup_age_hours(directory: Path) -> float | None:
    """Hours since the newest ``weaves-*.sql.gz`` was written."""
    files = sorted(directory.glob("weaves-*.sql.gz"), key=lambda p: p.stat().st_mtime)
    return round((time.time() - files[-1].stat().st_mtime) / 3600, 2) if files else None


def platform_status() -> dict[str, object] | None:
    """``platform_control.py status`` through the running API container."""
    command = [
        "docker", "compose", "-p", "weaves-single", "-f", str(COMPOSE),
        "--env-file", str(RUNTIME), "exec", "-T", "product-api", "python", "-",
        "status",
    ]  # fmt: skip
    script = (OPERATOR_DIR / "platform_control.py").read_text()
    done = subprocess.run(command, input=script, capture_output=True, text=True)
    return json.loads(done.stdout) if done.returncode == 0 else None


def main() -> int:
    """Print the state document."""
    env = read_env(RUNTIME)
    state = {
        "version": 1,
        "collected_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "env": {name: env[name] for name in PLAIN if name in env},
        "allowlist_set": any(env.get(name) for name in PRESENCE_ONLY),
        "newest_backup_age_hours": newest_backup_age_hours(BACKUPS),
        "offhost_backup_age_hours": None,
        "platform_controls": platform_status(),
    }
    print(json.dumps(state, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
