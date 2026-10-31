"""Compatibility exports for the persistence adapters' shared contracts."""

from application.ports.repository import Repository, TContract, _updated_at
from shared.errors import IdempotencyKeyAlreadyExists

__all__ = [
    "IdempotencyKeyAlreadyExists",
    "Repository",
    "TContract",
    "_updated_at",
]
