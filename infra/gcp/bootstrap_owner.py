"""Create the initial owner using the temporary onboarding-token gate."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any


def request(
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    token: str | None = None,
    onboarding_token: str | None = None,
    method: str | None = None,
) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if onboarding_token:
        headers["X-Weaves-Onboarding-Token"] = onboarding_token
    body = json.dumps(payload).encode() if payload is not None else None
    call = urllib.request.Request(
        url,
        data=body,
        headers=headers,
        method=method or ("POST" if body else "GET"),
    )
    try:
        with urllib.request.urlopen(call, timeout=45) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(
            f"Owner bootstrap returned HTTP {exc.code}: {detail}"
        ) from exc
    return json.loads(raw) if raw else {}


def main() -> int:
    api_base = os.environ["WEAVES_API_BASE"].rstrip("/")
    email = os.environ["WEAVES_OWNER_EMAIL"]
    password = os.environ["WEAVES_OWNER_PASSWORD"]
    onboarding_token = os.environ["WEAVES_ONBOARDING_TOKEN"]
    try:
        request(
            f"{api_base}/auth/login",
            payload={"email": email, "password": password},
        )
        print("Initial owner already exists and can sign in.")
        return 0
    except (urllib.error.HTTPError, RuntimeError):
        pass

    created = request(
        f"{api_base}/onboarding/organizations",
        payload={
            "name": "Weaves Production",
            "owner_email": email,
            "owner_display_name": "Weaves Admin",
        },
        onboarding_token=onboarding_token,
    )
    access_token = created.get("access_token")
    organization_id = created.get("organization", {}).get("id")
    if not access_token or not organization_id:
        raise RuntimeError("Organization onboarding returned an incomplete response")
    request(
        f"{api_base}/auth/password",
        payload={"password": password},
        token=access_token,
        method="PUT",
    )
    request(
        f"{api_base}/auth/login",
        payload={
            "email": email,
            "password": password,
            "organization_id": organization_id,
        },
    )
    print("Initial owner created; password login verified.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Owner bootstrap failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error
