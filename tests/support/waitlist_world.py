"""Shared helpers for the public waitlist tests."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient

from apps.api.app import create_app
from apps.runtime_wiring import compose_platform_runtime
from modules.waitlist.questions import CONTENT_ROOT, QuestionBank, digest

URL = "/api/v0/waitlist"
CONSENT = {"consent": True, "consent_version": "v1"}


def memory_runtime() -> Any:
    """A bootstrapped in-memory runtime with no demo seeding."""
    runtime = compose_platform_runtime(
        http_client=httpx.Client(), store=None, seed_local_defaults=False
    )
    runtime.bootstrap()
    return runtime


def client_for(runtime: Any, address: str = "203.0.113.5") -> TestClient:
    """A client that connects from one network address (the limiter source)."""
    if "_waitlist_test_app" not in runtime.__dict__:
        runtime.__dict__["_waitlist_test_app"] = create_app(runtime)
    app = runtime.__dict__["_waitlist_test_app"]
    client = TestClient(app, client=(address, 50000), raise_server_exceptions=False)
    client.__enter__()  # runs the lifespan that registers the routes
    return client


def join(client: TestClient, email: str, key: str | None = None, **extra: Any) -> Any:
    """POST a join with consent; ``extra`` overrides or adds body fields."""
    headers = {"Idempotency-Key": key} if key else {}
    return client.post(
        f"{URL}/submissions",
        json={"email": email, **CONSENT, **extra},
        headers=headers,
    )


def token_headers(token: str) -> dict[str, str]:
    """The only credential a waitlist submission accepts."""
    return {"X-Waitlist-Token": token}


def answer(client: TestClient, token: str, question_id: str, value: Any, **extra: Any):
    """PUT one answer."""
    return client.put(
        f"{URL}/submissions/current/answers/{question_id}",
        json={"value": value, **extra},
        headers=token_headers(token),
    )


def extended_bank(destination: Path) -> QuestionBank:
    """A bank with a published version 2 (the first question reworded)."""
    shutil.copytree(CONTENT_ROOT, destination)
    first = json.loads((destination / "questions/v1.json").read_text())
    first["version"] = 2
    first["questions"][0]["prompt"] = "Which region is your company based in?"
    (destination / "questions/v2.json").write_text(json.dumps(first))
    lock = json.loads((destination / "published.lock.json").read_text())
    lock["files"]["questions/v2.json"] = digest(destination / "questions/v2.json")
    (destination / "published.lock.json").write_text(json.dumps(lock))
    return QuestionBank.load(destination)


def stored_json(runtime: Any) -> str:
    """Everything persisted by the waitlist repositories, as one string."""
    service = runtime.waitlist_service
    rows = [*service.entries.list(), *service.submissions.list()]
    return json.dumps([row.model_dump(mode="json") for row in rows])
