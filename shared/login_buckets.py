"""Privacy-safe hashing for normalized login throttle buckets."""

import hashlib


def login_bucket_key(kind: str, value: str) -> str:
    """Hash a normalized login identifier before persisting its bucket key."""
    normalized = value.strip().casefold()
    return hashlib.sha256(f"weaves-login-v1\0{kind}\0{normalized}".encode()).hexdigest()
