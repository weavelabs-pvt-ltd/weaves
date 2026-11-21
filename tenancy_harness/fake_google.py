"""A fake Google OpenID Connect provider for the disposable tenancy stack.

It is an external-provider fake, like the model and MCP fakes: the application's
real Google sign-in code talks to it unchanged. It serves discovery, a JWKS
(RSA key with a kid, use=sig), an authorize endpoint and a token endpoint that
validates the code, redirect URI, client credentials and PKCE verifier before it
returns an RS256 ID token.

The authorize endpoint has no consent screen. It picks the person from the
``login_hint`` query parameter, else from the private control file
(``$TENANCY_GOOGLE_DIR/identity.json``) that host-side browser tests write, and
redirects straight back. Negative controls use ``fake_unverified=1`` (an
unverified email claim) and ``fake_deny=1`` (an ``access_denied`` redirect),
either as query parameters or as ``email_verified: false`` / ``deny: true`` in
the control file.
"""

import base64
import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

router = APIRouter(prefix="/google")
KID = "tenancy-fixture-key"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
CODES: dict[str, dict[str, str | bool]] = {}


def internal_base():
    """The URL the API container reaches (plain HTTP on the private network)."""
    return os.environ["TENANCY_GOOGLE_INTERNAL_URL"]


def public_origin():
    """The only origin the browser can reach: the harness reverse proxy."""
    return os.environ["TENANCY_PUBLIC_ORIGIN"]


def control_identity():
    """Read the control file the host writes; tolerate bind-mount lag."""
    control = Path(os.environ["TENANCY_GOOGLE_DIR"]) / "identity.json"
    for _ in range(40):
        try:
            return json.loads(control.read_text())
        except (OSError, ValueError):
            time.sleep(0.05)
    return {}


def person_from(request):
    """Pick who signs in: an explicit login_hint, else the control file."""
    query = request.query_params
    person = {} if query.get("login_hint") else control_identity()
    email = query.get("login_hint") or person.get("email", "")
    return {
        "email": email,
        "name": person.get("name") or "Fixture " + email.partition("@")[0],
        "verified": not (
            query.get("fake_unverified") == "1" or person.get("email_verified") is False
        ),
        "deny": query.get("fake_deny") == "1" or bool(person.get("deny")),
    }


def with_query(url, **values):
    """Append query parameters to a redirect URI that has none of its own."""
    return url + ("&" if "?" in url else "?") + urlencode(values)


@router.get("/.well-known/openid-configuration")
def discovery():
    """Publish internal token/JWKS URLs and a browser-reachable authorize URL."""
    return {
        "issuer": internal_base(),
        "authorization_endpoint": public_origin() + "/fake-google/authorize",
        "token_endpoint": internal_base() + "/token",
        "jwks_uri": internal_base() + "/jwks",
    }


@router.get("/identity")
def identity():
    """Echo the control file so the host can confirm the provider sees its choice."""
    return control_identity()


@router.get("/jwks")
def jwks():
    """Publish the public signing key."""
    public = jwt.algorithms.RSAAlgorithm.to_jwk(KEY.public_key(), as_dict=True)
    return {"keys": [{**public, "kid": KID, "use": "sig", "alg": "RS256"}]}


@router.get("/authorize")
def authorize(request: Request):
    """Approve immediately for the selected person; no consent UI."""
    query = request.query_params
    redirect_uri = query.get("redirect_uri", "")
    if (
        query.get("response_type") != "code"
        or query.get("client_id") != os.environ["TENANCY_GOOGLE_CLIENT_ID"]
        or query.get("code_challenge_method") != "S256"
        or not redirect_uri.startswith(public_origin() + "/api/v0/account/google/")
    ):
        return JSONResponse({"error": "invalid_request"}, status_code=400)
    person = person_from(request)
    if person["deny"]:
        error = {"error": "access_denied", "state": query.get("state", "")}
        return RedirectResponse(with_query(redirect_uri, **error), status_code=302)
    if "@" not in person["email"]:
        return JSONResponse({"error": "no fixture identity"}, status_code=400)
    code = secrets.token_urlsafe(24)
    CODES[code] = {
        **person,
        "nonce": query.get("nonce", ""),
        "challenge": query.get("code_challenge", ""),
        "redirect_uri": redirect_uri,
    }
    target = with_query(redirect_uri, code=code, state=query.get("state", ""))
    return RedirectResponse(target, status_code=302)


def pkce_matches(verifier, challenge):
    """Check the S256 code challenge against the presented verifier."""
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == challenge


@router.post("/token")
async def token(request: Request):
    """Redeem a code once, only with the right client, redirect and PKCE verifier."""
    form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
    record = CODES.pop(str(form.get("code", "")), None)
    valid = (
        record is not None
        and form.get("grant_type") == "authorization_code"
        and form.get("client_id") == os.environ["TENANCY_GOOGLE_CLIENT_ID"]
        and form.get("client_secret") == os.environ["TENANCY_GOOGLE_CLIENT_SECRET"]
        and form.get("redirect_uri") == record["redirect_uri"]
        and pkce_matches(str(form.get("code_verifier", "")), str(record["challenge"]))
    )
    if not valid or record is None:
        return Response(json.dumps({"error": "invalid_grant"}), status_code=400)
    return {"id_token": id_token(record), "access_token": "unused"}


def id_token(record):
    """Sign the claims Google would send for this person."""
    now = int(time.time())
    email = str(record["email"])
    subject = "11" + str(int(hashlib.sha256(email.encode()).hexdigest()[:12], 16))
    claims = {
        "iss": internal_base(),
        "aud": os.environ["TENANCY_GOOGLE_CLIENT_ID"],
        "sub": subject,
        "email": email,
        "email_verified": bool(record["verified"]),
        "name": record["name"],
        "nonce": record["nonce"],
        "iat": now,
        "exp": now + 3600,
    }
    return jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": KID})
