"""Deploy an immutable image through IAP with short-lived Google credentials."""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import time
from pathlib import Path
from uuid import uuid4

BUNDLE_FILES = (
    "apply_release.py",
    "release_runtime.py",
    "compose.yaml",
    "Caddyfile",
    "backup.sh",
)


# A fresh short-lived SSH key can take a few seconds to reach the VM. Retrying
# is safe only for this rejection, which happens before any remote command runs.
_KEY_NOT_YET_ACCEPTED = "Permission denied (publickey)"


def invoke(
    command: list[str],
    payload: str | None = None,
    *,
    attempts: int = 4,
    wait_seconds: float = 20.0,
) -> str:
    """Keep command output separate from transient access tokens."""
    for attempt in range(1, attempts + 1):
        result = subprocess.run(command, input=payload, text=True, capture_output=True)
        if not result.returncode:
            return result.stdout.strip()
        if _KEY_NOT_YET_ACCEPTED not in result.stderr or attempt == attempts:
            raise RuntimeError(result.stderr[-2000:])
        time.sleep(wait_seconds)
    raise AssertionError("unreachable")


def main() -> None:
    """Upload backend configuration; frontend releases use the same lock."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--component", required=True, choices=("backend", "frontend"))
    parser.add_argument("--image", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--project", default="weaves-510819")
    parser.add_argument("--account", default="shivamsawarn15@gmail.com")
    parser.add_argument("--zone", default="asia-south1-a")
    parser.add_argument("--vm", default="weaves-personal")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.revision):
        raise ValueError("Expected a full commit SHA")
    flags = [
        f"--project={args.project}",
        f"--account={args.account}",
        f"--zone={args.zone}",
        "--quiet",
        "--tunnel-through-iap",
        "--ssh-key-expire-after=15m",
    ]
    ssh = ["gcloud", "compute", "ssh", args.vm, *flags]
    stage = f"/tmp/weaves-release-{args.revision[:12]}-{uuid4().hex[:8]}"
    script = "/opt/weaves/singlevm/apply_release.py"
    bundle = []
    if args.component == "backend":
        invoke([*ssh, "--command=" + shlex.join(["mkdir", "-m", "700", stage])])
        source = Path(__file__).resolve().parent
        invoke(
            [
                "gcloud",
                "compute",
                "scp",
                *flags,
                *[str(source / name) for name in BUNDLE_FILES],
                f"{args.vm}:{stage}/",
            ]
        )
        script = f"{stage}/apply_release.py"
        bundle = ["--bundle", stage]
    command = shlex.join(
        [
            "sudo",
            "python3",
            "-B",
            script,
            "--component",
            args.component,
            "--image",
            args.image,
            "--revision",
            args.revision,
            *bundle,
        ]
    )
    try:
        token = invoke(
            [
                "gcloud",
                "auth",
                "print-access-token",
                f"--account={args.account}",
                "--quiet",
            ]
        )
        print(invoke([*ssh, "--command=" + command], token), flush=True)
    finally:
        if bundle:
            invoke(
                [*ssh, "--command=" + shlex.join(["sudo", "rm", "-rf", "--", stage])]
            )


if __name__ == "__main__":
    main()
