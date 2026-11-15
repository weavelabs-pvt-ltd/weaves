"""Generate the single-VM environment without changing migrated credentials."""

from __future__ import annotations

import json
import os
import secrets
import shlex
import sys
from pathlib import Path


def main() -> None:
    """Read the production encryption key securely from standard input."""
    incoming = json.load(sys.stdin)
    root = Path("/opt/weaves/singlevm")
    path = root / "runtime.env"
    os.umask(0o077)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists():
        values = dict(
            line.split("=", 1) for line in shlex.split(path.read_text(), comments=True)
        )
        if values["WEAVES_SECRET_ENCRYPTION_KEY"] != incoming["encryption_key"]:
            raise ValueError("Existing environment has a different encryption key")
    else:
        values = {
            "POSTGRES_PASSWORD": secrets.token_hex(32),
            "WEAVES_SECRET_ENCRYPTION_KEY": incoming["encryption_key"],
            "WEAVES_SSO_SESSION_SECRET": secrets.token_hex(32),
        }
    values.update(
        BACKEND_IMAGE=incoming["backend_image"],
        FRONTEND_IMAGE=incoming["frontend_image"],
    )
    path.write_text(
        "".join(f"{key}={shlex.quote(value)}\n" for key, value in values.items())
    )
    path.chmod(0o600)
    print("Single-VM runtime environment ready (root only).")


if __name__ == "__main__":
    main()
