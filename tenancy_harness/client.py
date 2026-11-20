"""Fixture client using public HTTP authentication and resource APIs."""

import hashlib
import os
import time

import httpx

API = "http://api:8000/api/v0"
PROVIDER = "https://provider"


def call(method, path, actor=None, body=None, expected=(200, 201, 202, 204)):
    """Call the actual API without logging tokens, codes, or credential bodies."""
    headers = actor["headers"] if actor else {}
    if path == "/onboarding/organizations":
        headers = {
            "X-Weaves-Onboarding-Token": os.environ[
                "WEAVES_ORGANIZATION_ONBOARDING_TOKEN"
            ]
        }
    response = httpx.request(method, API + path, headers=headers, json=body, timeout=30)
    if response.status_code not in expected:
        raise AssertionError(f"Fixture {method} {path}: HTTP {response.status_code}")
    return response.json() if response.status_code != 204 else None


def mailbox(email):
    """Retrieve synthetic mail from the network-isolated provider fake only."""
    response = httpx.get(PROVIDER + "/mail/" + email, timeout=10)
    response.raise_for_status()
    return response.json()


def sign_in(email, org_id):
    """Prove email through the real challenge, SMTP adapter, and verify endpoint."""
    # Distinct synthetic clients exercise native source throttling without disabling it.
    suffix = int(hashlib.sha256(email.encode()).hexdigest()[:2], 16) or 1
    source = {"headers": {"X-Forwarded-For": f"198.18.0.{suffix}"}}
    challenge = call(
        "POST",
        "/auth/email-code",
        source,
        body={"email": email, "organization_id": org_id},
    )
    proof = mailbox(email)["code"]
    if not proof:
        raise AssertionError("Real SMTP fixture did not receive a code")
    result = call(
        "POST",
        "/auth/email-verify",
        source,
        body={"challenge_id": challenge["challenge_id"], "code": proof},
    )
    return result["access_token"]


def actor(token, workspace, principal, email):
    """Describe a real persisted principal without inventing global memberships."""
    return {
        "headers": {"Authorization": "Bearer " + token, "X-Workspace-ID": workspace},
        "principal_id": principal,
        "email": email,
    }


def wait_for_services():
    """Wait for PostgreSQL-backed API and fake providers with a bounded timeout."""
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            health = httpx.get(API + "/health", timeout=3).json()
            fake = httpx.get(PROVIDER + "/health", timeout=3).json()
            if (
                health == {"status": "ok", "storage": "postgres"}
                and fake.get("fixture") == "tenancy-phase-0"
            ):
                return
        except (httpx.HTTPError, ValueError):
            pass
        time.sleep(1)
    raise RuntimeError("Disposable tenancy services failed to become ready")
