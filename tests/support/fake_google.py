"""A fake Google OpenID Connect provider served through httpx.MockTransport.

It signs ID tokens with a local RSA key and implements discovery, the token
endpoint (checking the client secret, redirect URI and PKCE verifier) and the
JWKS. ``authorize`` plays the part of the consent screen: it takes the
authorization URL the API redirected to and returns the callback query.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

CLIENT_ID = "test-client-id.apps.googleusercontent.com"
CLIENT_SECRET = "test-client-secret-value"
ISSUER = "https://accounts.google.com"
DISCOVERY = "https://accounts.google.com/.well-known/openid-configuration"
AUTHORIZE = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN = "https://oauth2.googleapis.com/token"
JWKS = "https://www.googleapis.com/oauth2/v3/certs"

_KEYS: list[rsa.RSAPrivateKey] = []


def _key(index: int) -> rsa.RSAPrivateKey:
    """Generate RSA keys lazily and reuse them across tests (they are slow)."""
    while len(_KEYS) <= index:
        _KEYS.append(rsa.generate_private_key(public_exponent=65537, key_size=2048))
    return _KEYS[index]


def _jwk(key: rsa.RSAPrivateKey, kid: str) -> dict[str, str]:
    """Publish a public key as a JWK."""
    public = jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
    return {**public, "kid": kid, "use": "sig", "alg": "RS256"}


class FakeGoogle:
    """One fake provider with a mutable key set and scripted failures."""

    def __init__(self) -> None:
        """Start with key one published and no pending authorizations."""
        self.key_index = 0
        self.kid = "kid-0"
        self.published = {self.kid: 0}
        self.codes: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []
        self.token_status = 200
        self.discovery_status = 200
        self.counter = 0

    def client(self) -> httpx.Client:
        """An httpx client whose only network is this fake."""
        return httpx.Client(transport=httpx.MockTransport(self._handle))

    def rotate(self, *, keep_old: bool = False) -> None:
        """Publish a new signing key and sign with it from now on."""
        self.key_index += 1
        self.kid = f"kid-{self.key_index}"
        if not keep_old:
            self.published = {}
        self.published[self.kid] = self.key_index

    def authorize(self, location: str, **overrides: Any) -> dict[str, str]:
        """Approve the authorization request and return the callback query.

        ``overrides`` replace claims in the ID token; the special keys
        ``sign_with`` (a key index) and ``kid`` change how it is signed.
        """
        query = {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}
        assert location.startswith(AUTHORIZE + "?")
        assert query["response_type"] == "code" and query["client_id"] == CLIENT_ID
        assert query["code_challenge_method"] == "S256"
        assert query["scope"] == "openid email profile"
        assert query["prompt"] == "select_account"
        self.counter += 1
        code = f"code-{self.counter}"
        self.codes[code] = {
            "nonce": query["nonce"],
            "challenge": query["code_challenge"],
            "redirect_uri": query["redirect_uri"],
            "overrides": overrides,
        }
        return {"code": code, "state": query["state"]}

    def claims(self, record: dict[str, Any]) -> dict[str, Any]:
        """The default ID token claims for one authorization, with overrides."""
        now = int(time.time())
        overrides = record["overrides"]
        email = overrides.get("email", "person@example.test")
        # Different people get different, stable subjects unless a test sets one.
        subject = "11" + str(
            int(hashlib.sha256(str(email).encode()).hexdigest()[:12], 16)
        )
        claims = {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "sub": subject,
            "email": email,
            "email_verified": True,
            "name": "Pat Person",
            "nonce": record["nonce"],
            "iat": now,
            "exp": now + 3600,
        }
        for key, value in record["overrides"].items():
            if key in {"sign_with", "kid"}:
                continue
            if value is None:
                claims.pop(key, None)
            else:
                claims[key] = value
        return claims

    def _id_token(self, record: dict[str, Any]) -> str:
        """Sign the claims, optionally with another key or a chosen ``kid``."""
        overrides = record["overrides"]
        index = overrides.get("sign_with", self.key_index)
        kid = overrides.get("kid", self.kid)
        return jwt.encode(
            self.claims(record),
            _key(index),
            algorithm="RS256",
            headers={"kid": kid},
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        """Route one request to discovery, the JWKS or the token endpoint."""
        url = str(request.url).split("?")[0]
        self.calls.append(f"{request.method} {url}")
        if url == DISCOVERY:
            if self.discovery_status != 200:
                return httpx.Response(self.discovery_status)
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": AUTHORIZE,
                    "token_endpoint": TOKEN,
                    "jwks_uri": JWKS,
                },
            )
        if url == JWKS:
            keys = [_jwk(_key(index), kid) for kid, index in self.published.items()]
            return httpx.Response(200, json={"keys": keys})
        if url == TOKEN and request.method == "POST":
            return self._token(request)
        return httpx.Response(404)

    def _token(self, request: httpx.Request) -> httpx.Response:
        """Redeem a code only with the right secret, redirect URI and verifier."""
        if self.token_status != 200:
            return httpx.Response(self.token_status, json={"error": "invalid_grant"})
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        record = self.codes.pop(form.get("code", ""), None)
        digest = hashlib.sha256(form.get("code_verifier", "").encode()).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        valid = (
            record is not None
            and form.get("grant_type") == "authorization_code"
            and form.get("client_id") == CLIENT_ID
            and form.get("client_secret") == CLIENT_SECRET
            and form.get("redirect_uri") == record["redirect_uri"]
            and challenge == record["challenge"]
        )
        if not valid or record is None:
            return httpx.Response(400, content=json.dumps({"error": "invalid_grant"}))
        return httpx.Response(
            200,
            json={"id_token": self._id_token(record), "access_token": "unused"},
        )
