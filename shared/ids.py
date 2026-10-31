"""Opaque identifiers shared across product modules."""

from uuid import uuid4


def new_id() -> str:
    return str(uuid4())
