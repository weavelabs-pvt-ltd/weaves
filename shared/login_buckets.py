"""Privacy-safe hashing for normalized login throttle buckets."""

import hashlib


def login_bucket_key(kind: str, value: str, scope: str | None = None) -> str:
    """Hash a normalized login identifier before persisting its bucket key.

    A tenant ``scope`` gives the same email independent allowances in each
    organization. Source-address buckets stay unscoped as the global limit.
    """
    normalized = value.strip().casefold()
    namespace = "" if scope is None else f"{scope}\0"
    return hashlib.sha256(
        f"weaves-login-v1\0{kind}\0{namespace}{normalized}".encode()
    ).hexdigest()
