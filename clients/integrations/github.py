"""Read-only GitHub search capability, bound to a workspace installation."""

from __future__ import annotations

import base64
import re
from time import monotonic
from typing import Any, Protocol
from urllib.parse import quote

import httpx

from contracts.v1 import PluginInstallation
from contracts.v1.json_data import thaw_json_value
from shared.errors import PluginGatewayError


class _InstallationTokenProvider(Protocol):
    def installation_token(
        self, installation_id: str, repositories: tuple[str, ...]
    ) -> str: ...


class GitHubReadPlugin:
    """Search issues and pull requests within configured repositories only."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        api_base_url: str = "https://api.github.com",
        timeout_seconds: float = 15.0,
        installation_token_provider: _InstallationTokenProvider | None = None,
    ) -> None:
        self._client = client
        self._api_base_url = api_base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._installation_tokens = installation_token_provider

    def healthcheck(self, installation: PluginInstallation) -> dict[str, str]:
        token = self._installation_token(installation)
        configuration = thaw_json_value(installation.configuration)
        repositories = configuration.get("repositories", [])
        if not repositories:
            raise PluginGatewayError(
                "plugin.configuration_invalid",
                "GitHub installation has no repositories",
            )
        repository = repositories[0]
        if (
            not isinstance(repository, str)
            or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None
        ):
            raise PluginGatewayError(
                "plugin.configuration_invalid",
                "GitHub repository configuration is invalid",
            )
        try:
            response = self._client.get(
                f"{self._api_base_url}/repos/{repository}",
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {token}",
                    "X-GitHub-Api-Version": "2026-03-10",
                },
                timeout=self._timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise PluginGatewayError(
                "plugin.timeout", "GitHub request timed out"
            ) from exc
        except httpx.HTTPError as exc:
            raise PluginGatewayError(
                "plugin.transport_error", "GitHub request failed"
            ) from exc
        if response.is_error:
            raise PluginGatewayError(
                "plugin.upstream_error", f"GitHub returned HTTP {response.status_code}"
            )
        try:
            full_name = response.json().get("full_name")
        except (ValueError, AttributeError) as exc:
            raise PluginGatewayError(
                "plugin.invalid_response", "GitHub returned an invalid response"
            ) from exc
        if not isinstance(full_name, str) or not full_name:
            raise PluginGatewayError(
                "plugin.invalid_response", "GitHub returned an invalid response"
            )
        return {"repository": full_name}

    def invoke_for_installation(
        self,
        capability_id: str,
        payload: dict[str, Any],
        installation: PluginInstallation,
    ) -> dict[str, Any]:
        if capability_id not in {
            "github.issues.search",
            "github.code.search",
        }:
            raise PluginGatewayError(
                "plugin.capability_unknown", "GitHub capability is not supported"
            )
        configuration = thaw_json_value(installation.configuration)
        repositories = configuration.get("repositories", [])
        if not repositories:
            raise PluginGatewayError(
                "plugin.configuration_invalid",
                "GitHub installation has no repositories",
            )
        token = self._installation_token(installation)

        query = payload["query"].strip()
        if not query:
            raise PluginGatewayError(
                "plugin.input_invalid", "GitHub search query is invalid"
            )
        if re.search(r"(?i)\b(?:repo|org|user):", query):
            raise PluginGatewayError(
                "plugin.query_scope_denied",
                "Repository scope is controlled by the installation",
            )
        documents: list[dict[str, Any]] = []
        deadline = monotonic() + self._timeout_seconds
        for repository in repositories:
            if len(documents) >= 5:
                break
            remaining = deadline - monotonic()
            if remaining <= 0:
                break
            items = self._search_repository(
                repository,
                query,
                token,
                remaining,
                len(documents),
                search_code=capability_id == "github.code.search",
            )
            document_builder = (
                self._code_documents
                if capability_id == "github.code.search"
                else self._issue_documents
            )
            documents.extend(document_builder(repository, items, 5 - len(documents)))
            if capability_id == "github.code.search" and not documents:
                documents.extend(
                    self._repository_file_documents(
                        repository, query, token, remaining, 5 - len(documents)
                    )
                )
        return {"documents": documents}

    def _search_repository(
        self,
        repository: str,
        query: str,
        token: str,
        remaining: float,
        count: int,
        *,
        search_code: bool,
    ) -> list[Any]:
        """Search one allowed repository and validate GitHub's response shape."""
        endpoint = "code" if search_code else "issues"
        accept = (
            "application/vnd.github.text-match+json"
            if search_code
            else "application/vnd.github+json"
        )
        try:
            response = self._client.get(
                f"{self._api_base_url}/search/{endpoint}",
                params={
                    "q": f"repo:{repository} {query}",
                    "per_page": min(5 - count, 5),
                },
                headers={
                    "Accept": accept,
                    "Authorization": f"Bearer {token}",
                    "X-GitHub-Api-Version": "2026-03-10",
                },
                timeout=min(5.0, remaining),
            )
        except httpx.TimeoutException as exc:
            raise PluginGatewayError(
                "plugin.timeout", "GitHub request timed out"
            ) from exc
        except httpx.HTTPError as exc:
            raise PluginGatewayError(
                "plugin.transport_error", "GitHub request failed"
            ) from exc
        if response.is_error:
            raise PluginGatewayError(
                "plugin.upstream_error", f"GitHub returned HTTP {response.status_code}"
            )
        try:
            items = response.json()["items"]
        except (ValueError, KeyError, TypeError) as exc:
            raise PluginGatewayError(
                "plugin.invalid_response", "GitHub returned an invalid response"
            ) from exc
        return items if isinstance(items, list) else []

    @staticmethod
    def _code_documents(
        repository: str, items: list[Any], remaining: int
    ) -> list[dict[str, Any]]:
        """Convert GitHub code matches into bounded, citable context records."""
        documents = []
        for item in items:
            if not isinstance(item, dict):
                continue
            path = item.get("path")
            url = item.get("html_url")
            if not isinstance(path, str) or not isinstance(url, str):
                continue
            matches = item.get("text_matches", [])
            snippets = (
                [
                    match["fragment"][:1200]
                    for match in matches
                    if isinstance(match, dict)
                    and isinstance(match.get("fragment"), str)
                ]
                if isinstance(matches, list)
                else []
            )
            text = f"Repository file: {path}. URL: {url}."
            if snippets:
                text += "\nMatching code excerpts:\n" + "\n---\n".join(snippets)
            documents.append(
                {
                    "source_id": f"github:{repository}:code:{path}",
                    "title": f"{repository}/{path}"[:160],
                    "text": text[:2000],
                }
            )
            if len(documents) >= remaining:
                break
        return documents

    def _repository_file_documents(
        self,
        repository: str,
        query: str,
        token: str,
        remaining_seconds: float,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Search bounded tracked files when GitHub code search is incomplete."""
        deadline = monotonic() + min(remaining_seconds, 8.0)
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2026-03-10",
        }
        metadata = self._get_json(
            f"{self._api_base_url}/repos/{repository}", headers, deadline
        )
        branch = metadata.get("default_branch")
        if not isinstance(branch, str) or not branch:
            return []
        tree_url = (
            f"{self._api_base_url}/repos/{repository}/git/trees/"
            f"{quote(branch, safe='')}?recursive=1"
        )
        tree = self._get_json(tree_url, headers, deadline)
        entries = tree.get("tree", [])
        if not isinstance(entries, list):
            return []
        terms = _query_terms(query)
        candidates = _rank_repository_files(entries, terms)
        documents = []
        for path, blob_url in candidates[:limit]:
            if monotonic() >= deadline:
                break
            file_data = self._get_json(blob_url, headers, deadline)
            content = file_data.get("content")
            encoding = file_data.get("encoding")
            if not isinstance(content, str) or encoding != "base64":
                continue
            try:
                source = base64.b64decode(content).decode("utf-8", errors="replace")
            except (ValueError, UnicodeError):
                continue
            excerpt = _repository_file_excerpt(source, terms)
            documents.append(
                {
                    "source_id": f"github:{repository}:code:{path}",
                    "title": f"{repository}/{path}"[:160],
                    "text": f"File: {path}.\n{excerpt}"[:2000],
                }
            )
        return documents

    def _get_json(
        self, url: str, headers: dict[str, str], deadline: float
    ) -> dict[str, Any]:
        """Fetch bounded GitHub JSON while preserving sanitized provider errors."""
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise PluginGatewayError("plugin.timeout", "GitHub request timed out")
        try:
            response = self._client.get(
                url, headers=headers, timeout=min(4.0, remaining)
            )
        except httpx.TimeoutException as exc:
            raise PluginGatewayError(
                "plugin.timeout", "GitHub request timed out"
            ) from exc
        except httpx.HTTPError as exc:
            raise PluginGatewayError(
                "plugin.transport_error", "GitHub request failed"
            ) from exc
        if response.is_error:
            raise PluginGatewayError(
                "plugin.upstream_error", f"GitHub returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise PluginGatewayError(
                "plugin.invalid_response", "GitHub returned an invalid response"
            ) from exc
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _issue_documents(
        repository: str, items: list[Any], remaining: int
    ) -> list[dict[str, Any]]:
        """Convert up to the remaining issue matches into context documents."""
        documents = []
        for item in items:
            document = _issue_document(repository, item)
            if document is not None:
                documents.append(document)
            if len(documents) >= remaining:
                break
        return documents

    def _installation_token(self, installation: PluginInstallation) -> str:
        configuration = thaw_json_value(installation.configuration)
        if configuration.get("auth_method") == "github_app":
            installation_id = configuration.get("github_installation_id")
            repositories = configuration.get("repositories", [])
            if (
                self._installation_tokens is None
                or not isinstance(installation_id, str)
                or not isinstance(repositories, list)
                or any(not isinstance(item, str) for item in repositories)
            ):
                raise PluginGatewayError(
                    "plugin.oauth_unavailable",
                    "GitHub App authentication is unavailable",
                )
            try:
                return self._installation_tokens.installation_token(
                    installation_id, tuple(repositories)
                )
            except PluginGatewayError:
                raise
            except Exception as exc:
                raise PluginGatewayError(
                    "plugin.credential_unavailable",
                    "GitHub installation token could not be issued",
                ) from exc
        raise PluginGatewayError(
            "plugin.oauth_unavailable",
            "GitHub connection is not configured as a GitHub App",
        )


def _issue_document(repository: str, item: Any) -> dict[str, Any] | None:
    """Convert one GitHub issue record into a bounded context document."""
    if not isinstance(item, dict):
        return None
    number = item.get("number")
    title = item.get("title")
    if not isinstance(number, int) or not isinstance(title, str):
        return None
    body = item.get("body") or ""
    if not isinstance(body, str):
        body = ""
    state = item.get("state", "unknown")
    text = (
        f"Issue or pull request state: {state}. "
        f"URL: {item.get('html_url') or 'unavailable'}.\n{body[:1500]}"
    )[:2000]
    return {
        "source_id": f"github:{repository}#{number}",
        "title": f"{repository} #{number}: {title}"[:160],
        "text": text,
    }


_CODE_FILE_SUFFIXES = {
    ".css",
    ".go",
    ".html",
    ".java",
    ".js",
    ".jsx",
    ".json",
    ".kt",
    ".md",
    ".py",
    ".rs",
    ".sql",
    ".swift",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}
_QUERY_STOP_WORDS = {
    "access",
    "about",
    "can",
    "code",
    "find",
    "for",
    "from",
    "how",
    "into",
    "repo",
    "repository",
    "search",
    "the",
    "this",
    "where",
    "with",
}
_SENSITIVE_PATH_MARKERS = {
    ".env",
    "credential",
    "private_key",
    "private-key",
    "secret",
    "token",
}


def _query_terms(query: str) -> tuple[str, ...]:
    """Return a small set of useful lowercase terms from a natural query."""
    terms = {
        item
        for item in re.findall(r"[a-zA-Z0-9_]{3,}", query.lower())
        if item not in _QUERY_STOP_WORDS
    }
    return tuple(sorted(terms))[:12]


def _rank_repository_files(
    entries: list[Any], terms: tuple[str, ...]
) -> list[tuple[str, str]]:
    """Rank safe, bounded source paths against terms from the user task."""
    candidates: list[tuple[int, str, str]] = []
    readmes: list[tuple[int, str, str]] = []
    allowed_extensions = {suffix.removeprefix(".") for suffix in _CODE_FILE_SUFFIXES}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("type") != "blob":
            continue
        path = entry.get("path")
        size = entry.get("size")
        if not isinstance(path, str) or not isinstance(size, int) or size > 150_000:
            continue
        lowered = path.lower()
        if any(marker in lowered for marker in _SENSITIVE_PATH_MARKERS):
            continue
        if "." not in path or path.rsplit(".", 1)[-1].lower() not in allowed_extensions:
            continue
        url = entry.get("url")
        if not isinstance(url, str):
            continue
        score = sum(term in lowered for term in terms)
        record = (score, path, url)
        if score:
            candidates.append(record)
        elif path.rsplit("/", 1)[-1].lower().startswith("readme."):
            readmes.append(record)
    if candidates:
        candidates.sort(key=lambda item: (-item[0], len(item[1]), item[1]))
    else:
        candidates = sorted(readmes, key=lambda item: item[1])
    return [(path, url) for _, path, url in candidates]


def _repository_file_excerpt(source: str, terms: tuple[str, ...]) -> str:
    """Extract a bounded excerpt around the first matching query term."""
    lowered = source.lower()
    positions = [lowered.find(term) for term in terms if lowered.find(term) >= 0]
    start = max(0, min(positions) - 400) if positions else 0
    excerpt = source[start : start + 1600]
    if start:
        excerpt = "…" + excerpt
    if start + len(excerpt) < len(source):
        excerpt += "…"
    return excerpt
