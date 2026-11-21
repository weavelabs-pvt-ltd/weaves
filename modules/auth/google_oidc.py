"""Google OpenID Connect: configuration, discovery, token exchange, ID tokens.

Everything here is provider mechanics. It knows nothing about accounts. A
failure is one of two kinds so callers can answer without leaking details:
``ProviderRejected`` (the proof is bad) and ``ProviderUnavailable`` (the
provider could not be reached or answered nonsense).
"""

from __future__ import annotations

import hmac
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
import jwt

from modules.auth.http.browser_sessions import browser_origin

DEFAULT_DISCOVERY_URL = "https://accounts.google.com/.well-known/openid-configuration"
CALLBACK_PATH = "/api/v0/account/google/callback"
_GOOGLE_ISSUER = "https://accounts.google.com"
_CLOCK_SKEW_SECONDS = 60
_REQUEST_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
_DISCOVERY_TTL = 24 * 3600
_JWKS_TTL = 3600
_JWKS_MIN_REFRESH = 5.0


class ProviderRejected(Exception):
    """The authorization code or ID token is not acceptable proof."""


class ProviderUnavailable(Exception):
    """Google could not be reached or returned an unusable answer."""


def _csv(name: str) -> frozenset[str]:
    """Read a comma-separated, case-insensitive allowlist variable."""
    return frozenset(
        item.strip().casefold()
        for item in os.environ.get(name, "").split(",")
        if item.strip()
    )


@dataclass(frozen=True)
class GoogleConfig:
    """Google sign-in settings, read once at startup."""

    client_id: str
    client_secret: str
    redirect_uri: str
    discovery_url: str
    allowed_emails: frozenset[str]
    allowed_domains: frozenset[str]

    @classmethod
    def from_env(cls) -> GoogleConfig | None:
        """Return the configuration, or None when sign-in is not configured."""
        client_id = os.environ.get("WEAVES_GOOGLE_CLIENT_ID", "").strip()
        client_secret = os.environ.get("WEAVES_GOOGLE_CLIENT_SECRET", "").strip()
        if not client_id or not client_secret:
            return None
        return cls(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=browser_origin() + CALLBACK_PATH,
            discovery_url=os.environ.get("WEAVES_GOOGLE_DISCOVERY_URL", "").strip()
            or DEFAULT_DISCOVERY_URL,
            allowed_emails=_csv("WEAVES_ACCOUNT_ALLOWED_EMAILS"),
            allowed_domains=frozenset(
                domain.lstrip("@")
                for domain in _csv("WEAVES_ACCOUNT_ALLOWED_EMAIL_DOMAINS")
            ),
        )

    def allows(self, email: str) -> bool:
        """Apply the allowlist; with none configured any verified email passes."""
        if not self.allowed_emails and not self.allowed_domains:
            return True
        address = email.strip().casefold()
        return (
            address in self.allowed_emails
            or address.rpartition("@")[2] in self.allowed_domains
        )


@dataclass(frozen=True)
class GoogleClaims:
    """The verified facts about a person that sign-in needs."""

    issuer: str
    subject: str
    email: str
    name: str


@dataclass(frozen=True)
class _Discovery:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str


class GoogleOidcProvider:
    """Talk to Google (or a test double) with bounded, cached requests."""

    def __init__(
        self,
        config: GoogleConfig,
        http: httpx.Client,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Bind the settings and the shared HTTP client."""
        self._config = config
        self._http = http
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._discovery: _Discovery | None = None
        self._discovered_at = 0.0
        self._keys: dict[str, Mapping[str, Any]] = {}
        self._keys_at: float | None = None

    def authorization_url(self, state: str, nonce: str, challenge: str) -> str:
        """Build the Google authorization redirect for one sign-in attempt."""
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self._config.client_id,
                "redirect_uri": self._config.redirect_uri,
                "scope": "openid email profile",
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "prompt": "select_account",
            }
        )
        return f"{self._discover().authorization_endpoint}?{query}"

    def redeem(self, code: str, verifier: str, nonce: str) -> GoogleClaims:
        """Exchange the code, verify the ID token and return its claims."""
        discovery = self._discover()
        id_token = self._exchange(discovery, code, verifier)
        return self._verify(discovery, id_token, nonce)

    def _get_json(self, url: str) -> dict[str, Any]:
        """Fetch one JSON document from the provider."""
        try:
            response = self._http.get(url, timeout=_REQUEST_TIMEOUT)
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderUnavailable("provider request failed") from exc
        if response.status_code != 200 or not isinstance(body, dict):
            raise ProviderUnavailable("provider answered with an error")
        return body

    def _discover(self) -> _Discovery:
        """Return the cached discovery document, refreshing it daily."""
        with self._lock:
            now = self._monotonic()
            if (
                self._discovery is not None
                and now - self._discovered_at < _DISCOVERY_TTL
            ):
                return self._discovery
            document = self._get_json(self._config.discovery_url)
            secure = urlsplit(self._config.discovery_url).scheme == "https"
            values: dict[str, str] = {}
            for key in (
                "issuer",
                "authorization_endpoint",
                "token_endpoint",
                "jwks_uri",
            ):
                value = document.get(key)
                if not isinstance(value, str) or not value:
                    raise ProviderUnavailable("discovery document is incomplete")
                if secure and not value.startswith("https://"):
                    raise ProviderUnavailable("discovery document is not https")
                values[key] = value
            self._discovery = _Discovery(**values)
            self._discovered_at = now
            return self._discovery

    def _exchange(self, discovery: _Discovery, code: str, verifier: str) -> str:
        """Redeem the authorization code with the client secret and PKCE verifier."""
        try:
            response = self._http.post(
                discovery.token_endpoint,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self._config.redirect_uri,
                    "client_id": self._config.client_id,
                    "client_secret": self._config.client_secret,
                    "code_verifier": verifier,
                },
                headers={"Accept": "application/json"},
                timeout=_REQUEST_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise ProviderUnavailable("token request failed") from exc
        if 400 <= response.status_code < 500:
            raise ProviderRejected("code refused")
        try:
            id_token = response.json().get("id_token")
        except (ValueError, AttributeError) as exc:
            raise ProviderUnavailable("token response unreadable") from exc
        if response.status_code != 200:
            raise ProviderUnavailable("token endpoint failed")
        if not isinstance(id_token, str) or not id_token:
            raise ProviderRejected("no ID token")
        return id_token

    def _signing_key(self, discovery: _Discovery, kid: str) -> Any:
        """Find a signing key, refetching the JWKS once for an unknown ``kid``."""
        with self._lock:
            now = self._monotonic()
            stale = self._keys_at is None or now - self._keys_at >= _JWKS_TTL
            unknown = kid not in self._keys
            cooled = self._keys_at is None or now - self._keys_at >= _JWKS_MIN_REFRESH
            if stale or (unknown and cooled):
                keys = self._get_json(discovery.jwks_uri).get("keys")
                if not isinstance(keys, list):
                    raise ProviderUnavailable("JWKS is unreadable")
                self._keys = {
                    str(item["kid"]): item
                    for item in keys
                    if isinstance(item, dict) and item.get("kid")
                }
                self._keys_at = now
            jwk = self._keys.get(kid)
        if jwk is None or jwk.get("kty") != "RSA" or jwk.get("use", "sig") != "sig":
            raise ProviderRejected("unknown signing key")
        try:
            return jwt.PyJWK(dict(jwk)).key
        except jwt.PyJWTError as exc:
            raise ProviderRejected("unusable signing key") from exc

    def _verify(self, discovery: _Discovery, id_token: str, nonce: str) -> GoogleClaims:
        """Verify signature, issuer, audience, times, nonce and the email claims."""
        try:
            header = jwt.get_unverified_header(id_token)
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
                raise ProviderRejected("unsupported ID token header")
            claims = jwt.decode(
                id_token,
                self._signing_key(discovery, header["kid"]),
                algorithms=["RS256"],
                audience=self._config.client_id,
                leeway=_CLOCK_SKEW_SECONDS,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise ProviderRejected("ID token is invalid") from exc
        issuers = {discovery.issuer}
        if discovery.issuer == _GOOGLE_ISSUER:
            issuers.add("accounts.google.com")
        if claims.get("iss") not in issuers:
            raise ProviderRejected("wrong issuer")
        token_nonce = claims.get("nonce")
        if not isinstance(token_nonce, str) or not hmac.compare_digest(
            token_nonce, nonce
        ):
            raise ProviderRejected("wrong nonce")
        return _claims(discovery.issuer, claims)


def _claims(issuer: str, claims: Mapping[str, Any]) -> GoogleClaims:
    """Extract the subject and a verified email, or reject the token."""
    subject, email = claims.get("sub"), claims.get("email")
    if not isinstance(subject, str) or not 3 <= len(subject) <= 320:
        raise ProviderRejected("missing subject")
    if claims.get("email_verified") not in (True, "true"):
        raise ProviderRejected("email is not verified")
    address = email.strip().casefold() if isinstance(email, str) else ""
    if "@" not in address or not 3 <= len(address) <= 320:
        raise ProviderRejected("missing email")
    name = claims.get("name")
    return GoogleClaims(
        issuer=issuer,
        subject=subject,
        email=address,
        name=name.strip() if isinstance(name, str) else "",
    )
