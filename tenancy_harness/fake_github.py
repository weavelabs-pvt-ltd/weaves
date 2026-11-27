"""A small fake GitHub REST API (opt-in) for the Slack PR review tests and harness.

An external-provider fake like ``fake_slack.py``: product code talks to it unchanged
with ``WEAVES_GITHUB_API_BASE_URL`` pointing at ``/github``. It holds no real
credentials (the installation token is a made-up constant, the App JWT is only
checked for its shape) and serves exactly what a review reads:

* ``POST /github/app/installations/{id}/access_tokens``
* ``GET  /github/repos/{owner}/{repo}`` (the connector's health probe)
* ``GET  /github/repos/{owner}/{repo}/pulls/{n}`` and ``.../files`` (paginated)

Every other path answers 404 and is recorded in ``unexpected``: a review must never
fetch anything outside its pull request. ``/github/_control/{calls,reset}`` expose
the recorded requests (path and query only, never a header).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from tenancy_harness import fake_github_data as data

TOKEN = "ghs_fixture_installation_token"


@dataclass
class GitHubState:
    """What the fake was asked (no credentials)."""

    calls: list[dict[str, Any]] = field(default_factory=list)
    unexpected: list[str] = field(default_factory=list)

    def reset(self) -> None:
        """Forget the recorded requests."""
        self.calls.clear()
        self.unexpected.clear()


STATE = GitHubState()
_PULL = re.compile(r"^/repos/([\w.-]+/[\w.-]+)/pulls/(\d+)(/files)?$")
_REPO = re.compile(r"^/repos/([\w.-]+/[\w.-]+)$")
_TOKENS = re.compile(r"^/app/installations/(\d+)/access_tokens$")


def handle(
    method: str, path: str, params: dict[str, str], authorization: str, body: Any
) -> tuple[int, Any, dict[str, str]]:
    """Answer one request: ``(status, json, headers)``."""
    STATE.calls.append({"method": method, "path": path, "params": dict(params)})
    token = _TOKENS.match(path)
    if method == "POST" and token:
        if authorization.count(".") != 2:  # a JWT-shaped bearer, nothing more
            return 401, {"message": "Bad credentials"}, {}
        STATE.calls[-1]["repositories"] = list((body or {}).get("repositories", []))
        return (
            201,
            {"token": TOKEN, "expires_at": "2099-01-01T00:00:00Z"},
            {},
        )
    if authorization != f"Bearer {TOKEN}":
        return 401, {"message": "Bad credentials"}, {}
    repo = _REPO.match(path)
    if method == "GET" and repo and repo.group(1).lower() in data.COVERED:
        return 200, {"full_name": repo.group(1)}, {}
    pull = _PULL.match(path)
    if method == "GET" and pull:
        return _pull(pull.group(1), int(pull.group(2)), bool(pull.group(3)), params)
    STATE.unexpected.append(f"{method} {path}")
    return 404, {"message": "Not Found"}, {}


def _pull(
    repo: str, number: int, files: bool, params: dict[str, str]
) -> tuple[int, Any, dict[str, str]]:
    status = data.ERRORS.get((repo.lower(), number))
    if status == 429:
        return 429, {"message": "rate limit"}, {"retry-after": "60"}
    if status in (403, 500):
        return int(status), {"message": "internal secret text"}, {}
    if status == 404:
        return 404, {"message": "Not Found"}, {}
    payload = data.pull_payload(repo, number)
    if payload is None:
        return 404, {"message": "Not Found"}, {}
    if not files:
        return 200, payload, {}
    page = int(params.get("page", "1"))
    per_page = min(int(params.get("per_page", "30")), 100)
    return 200, data.files_page(repo, number, page, per_page), {}


router = APIRouter(prefix="/github")


@router.api_route("/{path:path}", methods=["GET", "POST"])
async def github(path: str, request: Request) -> JSONResponse:
    """The fake GitHub REST API and its control surface."""
    if path == "_control/calls":
        return JSONResponse(
            {"calls": STATE.calls, "unexpected": STATE.unexpected}, status_code=200
        )
    if path == "_control/reset":
        STATE.reset()
        return JSONResponse({"ok": True})
    body = None
    if request.method == "POST":
        try:
            body = await request.json()
        except ValueError:
            body = None
    status, payload, headers = handle(
        request.method,
        "/" + path,
        dict(request.query_params),
        request.headers.get("authorization", ""),
        body,
    )
    return JSONResponse(payload, status_code=status, headers=headers)
