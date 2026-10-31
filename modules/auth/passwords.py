"""Password hashing helpers for local user authentication."""

from __future__ import annotations

import hashlib
import hmac
import secrets

_PASSWORD_HASH_ITERATIONS = 310_000


def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, _PASSWORD_HASH_ITERATIONS
    )
    return f"pbkdf2_sha256${_PASSWORD_HASH_ITERATIONS}${salt.hex()}${digest.hex()}"


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, iteration_text, salt_hex, digest_hex = encoded.split("$")
        iterations = int(iteration_text)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (ValueError, TypeError):
        return False
    if (
        algorithm != "pbkdf2_sha256"
        or iterations != _PASSWORD_HASH_ITERATIONS
        or len(salt) != 16
        or len(expected) != 32
    ):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


_DUMMY_PASSWORD_HASH = _hash_password("weaves-invalid-account-password")
