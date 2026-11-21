"""Who performed a lifecycle change, and validation of operator-supplied text."""

from __future__ import annotations

import hashlib

from contracts.v1 import LifecycleActorKind

SYSTEM_ACTOR = "system:lifecycle-worker"


class Actor:
    """Who is acting: kind, a stable reference and the audit principal id."""

    def __init__(self, kind: LifecycleActorKind, ref: str, audit_id: str) -> None:
        """Bind the kind, human-readable reference and valid audit actor id."""
        self.kind = kind
        self.ref = ref
        self.audit_id = audit_id

    @classmethod
    def operator(cls, email: str) -> Actor:
        """An operator is attributed by their stated address, never a principal."""
        ref = email.strip()
        if not ref or len(ref) > 320 or not ref.isprintable():
            raise ValueError("--operator must be a printable address up to 320 chars")
        audit = f"operator:{ref}"
        if len(audit) > 128:
            audit = "operator:" + hashlib.sha256(ref.encode()).hexdigest()[:32]
        return cls(LifecycleActorKind.OPERATOR, ref, audit)

    @classmethod
    def owner(cls, principal_id: str) -> Actor:
        """The owner acts as their tenant-local principal."""
        return cls(LifecycleActorKind.OWNER, principal_id, principal_id)

    @classmethod
    def system(cls) -> Actor:
        """The worker acts as a fixed system actor."""
        return cls(LifecycleActorKind.SYSTEM, SYSTEM_ACTOR, SYSTEM_ACTOR)


def clean_reason(reason: str | None, *, required: bool) -> str | None:
    """Validate an optional or required printable reason."""
    text = (reason or "").strip()
    if not text:
        if required:
            raise ValueError("A reason is required")
        return None
    if len(text) > 500 or not text.isprintable():
        raise ValueError("The reason must be printable text up to 500 characters")
    return text
