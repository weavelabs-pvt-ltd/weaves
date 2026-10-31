"""Auth-specific normalization for privacy-safe throttle bucket identifiers."""

import hashlib


def login_bucket_key(kind: str, value: str) -> str:
    """Hash a normalized login identifier before it is stored as a bucket key."""
    normalized = value.strip().casefold()
    return hashlib.sha256(f"weaves-login-v1\0{kind}\0{normalized}".encode()).hexdigest()
