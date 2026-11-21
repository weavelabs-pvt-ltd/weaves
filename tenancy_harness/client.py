"""Fixture client using public HTTP authentication and resource APIs."""

import hashlib
import time

import httpx

API = "http://api:8000/api/v0"
PROVIDER = "https://provider"


def call(method, path, actor=None, body=None, expected=(200, 201, 202, 204)):
    """Call the actual API without logging tokens, codes, or credential bodies."""
    headers = actor["headers"] if actor else {}
    response = httpx.request(method, API + path, headers=headers, json=body, timeout=30)
    if response.status_code not in expected:
        raise AssertionError(f"Fixture {method} {path}: HTTP {response.status_code}")
    return response.json() if response.status_code != 204 else None


def mailbox(email):
    """Retrieve synthetic mail from the network-isolated provider fake only."""
    response = httpx.get(PROVIDER + "/mail/" + email, timeout=10)
    response.raise_for_status()
    return response.json()


def source_for(email):
    """Give each synthetic person a stable client address so native throttles apply."""
    digest = hashlib.sha256(email.encode()).digest()
    return {"X-Forwarded-For": f"198.18.{digest[0]}.{digest[1] % 250 + 1}"}


def account_sign_in(email):
    """Prove an email through the account challenge, SMTP adapter and verify routes."""
    source = {"headers": source_for(email)}
    challenge = call("POST", "/account/email-code", source, {"email": email})
    proof = mailbox(email)["code"]
    if not proof:
        raise AssertionError("Real SMTP fixture did not receive a code")
    result = call(
        "POST",
        "/account/email-verify",
        source,
        {"challenge_id": challenge["challenge_id"], "code": proof},
    )
    return result["access_token"]


def actor(token, org_id, workspace, principal, email):
    """Describe an account acting as one tenant-local principal in one organization."""
    return {
        "headers": {
            "Authorization": "Bearer " + token,
            "X-Organization-Id": org_id,
            "X-Workspace-ID": workspace,
        },
        "principal_id": principal,
        "email": email,
    }


def service_actor(token, workspace, principal):
    """A workspace-bound service account uses its API credential as a bearer."""
    return {
        "headers": {"Authorization": "Bearer " + token, "X-Workspace-ID": workspace},
        "principal_id": principal,
        "email": "service",
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
