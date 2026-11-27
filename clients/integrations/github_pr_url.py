"""Strict parsing of a GitHub pull request URL (nothing else is ever fetched).

A pull request is named by exactly one ``https://github.com/<owner>/<repo>/pull/<n>``
token, optionally followed by one of the tab paths (``/files``, ``/commits``,
``/checks``), a query or a fragment. Everything else is refused: other hosts and
look-alikes (``github.com.evil.test``, ``user@github.com``, ports, ``www.``),
``http``, relative paths, dot segments, percent-encoded path parts, non-ASCII
digits or letters, several different pull requests in one text. Owner and
repository are normalised to lower case for allowlist comparisons.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_URL = re.compile(
    r"https://github\.com/"
    r"(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/"
    r"(?P<repo>[A-Za-z0-9._-]{1,100})/pull/(?P<number>[1-9][0-9]{0,6})"
    r"(?:/(?:files|commits|checks))?/?"
    r"(?:\?[A-Za-z0-9._~=&%+,;:@!$'*-]{0,200})?"
    r"(?:#[A-Za-z0-9._~=&%+,;:@!$'*/-]{0,200})?",
    re.ASCII | re.IGNORECASE,
)
_TRAILING = ".,;:!)]}>'\""
_LEADING = "([{<'\""


@dataclass(frozen=True)
class PullRequestRef:
    """A validated pull request, with owner and repository in lower case."""

    owner: str
    repo: str
    number: int

    @property
    def full_name(self) -> str:
        """``owner/repo`` in lower case (the allowlist and coverage key)."""
        return f"{self.owner}/{self.repo}"

    @property
    def ref(self) -> str:
        """``owner/repo#number``."""
        return f"{self.full_name}#{self.number}"

    @property
    def url(self) -> str:
        """The canonical pull request URL."""
        return f"https://github.com/{self.full_name}/pull/{self.number}"


def _candidates(text: str) -> list[str]:
    tokens: list[str] = []
    for raw in text.split():
        token = raw.strip(_LEADING).rstrip(_TRAILING)
        lowered = token.lower()
        if lowered.startswith(("https://", "http://")) or "github.com" in lowered:
            tokens.append(token)
    return tokens


def parse_pull_request_url(text: str) -> PullRequestRef | None:
    """The one pull request named in ``text``, or ``None``.

    Any token that looks like a link must itself be a valid pull request URL and
    all such tokens must name the same pull request; one stray or hostile link
    makes the whole text unparseable (fail closed).
    """
    found: set[PullRequestRef] = set()
    for token in _candidates(text):
        match = _URL.fullmatch(token)
        if match is None:
            return None
        repo = match.group("repo").lower()
        if repo in (".", "..") or repo.endswith(".git"):
            return None
        found.add(
            PullRequestRef(
                match.group("owner").lower(), repo, int(match.group("number"))
            )
        )
    return next(iter(found)) if len(found) == 1 else None
