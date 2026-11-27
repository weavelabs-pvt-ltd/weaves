"""Append-only, hash-chained tombstone log kept outside the database.

A tombstone records that something irreversible happened (an organization was
purged, a credential or secret was revoked) so that restoring an older database
backup can re-apply it. Tombstones hold ids, kinds and timestamps only: no
secrets, tokens, names, emails or tenant content. The credential digest field is
a SHA-256 of the stored token digest, never the token.

Storage today is one JSON-lines file on the VM disk (``WEAVES_TOMBSTONE_DIR``,
``tombstones.jsonl``). Every line carries a sequence number and the SHA-256 hash
of the previous line, so truncation or editing is detectable and a shipper can
copy it to off-host storage by sequence number without parsing anything else.
This module is standard-library only so host scripts can import it.

Failure semantics: ``record`` raises ``TombstoneWriteError`` and never swallows
a failed write. Callers decide the order (see ``docs/phase-6-design.md``).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

LOG_NAME = "tombstones.jsonl"
GENESIS = "0" * 64
ORGANIZATION_PURGE_STARTED = "organization_purge_started"
ORGANIZATION_DELETED = "organization_deleted"
CREDENTIAL_REVOKED = "credential_revoked"
SECRET_DELETED = "secret_deleted"
SECRET_REPLACED = "secret_replaced"
SLACK_INSTALLATION_ENDED = "slack_installation_ended"
KINDS = frozenset(
    {
        ORGANIZATION_PURGE_STARTED,
        ORGANIZATION_DELETED,
        CREDENTIAL_REVOKED,
        SECRET_DELETED,
        SECRET_REPLACED,
        SLACK_INSTALLATION_ENDED,
    }
)
_FIELDS = {
    "v",
    "seq",
    "id",
    "kind",
    "org_id",
    "subject_kind",
    "subject_id",
    "subject_digest",
    "at",
    "erased",
    "prev",
}


class TombstoneWriteError(RuntimeError):
    """The tombstone could not be made durable."""


class TombstoneLogCorrupt(RuntimeError):
    """The log is unreadable, edited or has a broken hash chain."""


@dataclass(frozen=True)
class Tombstone:
    """One verified log line."""

    seq: int
    id: str
    kind: str
    org_id: str
    subject_kind: str
    subject_id: str
    subject_digest: str
    at: str
    erased: tuple[str, ...]
    hash: str


class TombstoneSink(Protocol):
    """What application code needs: record one irreversible fact."""

    def record(
        self,
        kind: str,
        org_id: str,
        *,
        subject_kind: str = "",
        subject_id: str = "",
        subject_digest: str = "",
        erased: tuple[str, ...] = (),
    ) -> None:
        """Make the tombstone durable or raise ``TombstoneWriteError``."""
        ...


class NullTombstones:
    """Used when ``WEAVES_TOMBSTONE_DIR`` is unset (development, plain tests)."""

    def record(
        self,
        kind: str,
        org_id: str,
        *,
        subject_kind: str = "",
        subject_id: str = "",
        subject_digest: str = "",
        erased: tuple[str, ...] = (),
    ) -> None:
        """Do nothing."""


def digest_of(value: str) -> str:
    """SHA-256 of a stored token digest, so a tombstone never names a token."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _line_hash(body: dict[str, Any]) -> str:
    text = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse(raw: bytes, previous: str, expected_seq: int) -> Tombstone:
    try:
        data = json.loads(raw)
        claimed = data.pop("hash")
        if set(data) != _FIELDS or data["kind"] not in KINDS:
            raise ValueError("unexpected fields")
        if data["prev"] != previous or data["seq"] != expected_seq:
            raise ValueError("chain broken")
        if _line_hash(data) != claimed:
            raise ValueError("hash mismatch")
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise TombstoneLogCorrupt(f"tombstone {expected_seq}: {exc}") from exc
    return Tombstone(
        seq=data["seq"],
        id=data["id"],
        kind=data["kind"],
        org_id=data["org_id"],
        subject_kind=data["subject_kind"],
        subject_id=data["subject_id"],
        subject_digest=data["subject_digest"],
        at=data["at"],
        erased=tuple(data["erased"]),
        hash=claimed,
    )


def _scan(handle: Any) -> list[Tombstone]:
    handle.seek(0)
    entries: list[Tombstone] = []
    previous = GENESIS
    for raw in handle.read().splitlines():
        if not raw.strip():
            continue
        entry = _parse(raw, previous, len(entries) + 1)
        entries.append(entry)
        previous = entry.hash
    return entries


class FileTombstoneLog:
    """The on-disk log. Safe for concurrent writers via an exclusive file lock."""

    def __init__(self, directory: str | Path) -> None:
        """Bind the directory; it is created on first write."""
        self.directory = Path(directory)
        self.path = self.directory / LOG_NAME

    @staticmethod
    def identity(kind: str, org_id: str, subject_kind: str, subject_id: str) -> str:
        """Stable id so recording the same fact twice appends once."""
        text = "|".join((kind, org_id, subject_kind, subject_id))
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]

    def record(
        self,
        kind: str,
        org_id: str,
        *,
        subject_kind: str = "",
        subject_id: str = "",
        subject_digest: str = "",
        erased: tuple[str, ...] = (),
    ) -> None:
        """Append one tombstone (idempotent by identity) and fsync it."""
        if kind not in KINDS:
            raise ValueError(f"unknown tombstone kind {kind!r}")
        ident = self.identity(kind, org_id, subject_kind, subject_id)
        try:
            self.directory.mkdir(parents=True, exist_ok=True, mode=0o750)
            fd = os.open(self.path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o640)
            with os.fdopen(fd, "r+b") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                entries = _scan(handle)
                if any(entry.id == ident for entry in entries):
                    return
                body = {
                    "v": 1,
                    "seq": len(entries) + 1,
                    "id": ident,
                    "kind": kind,
                    "org_id": org_id,
                    "subject_kind": subject_kind,
                    "subject_id": subject_id,
                    "subject_digest": subject_digest,
                    "at": datetime.now(UTC).isoformat(),
                    "erased": list(erased),
                    "prev": entries[-1].hash if entries else GENESIS,
                }
                line = json.dumps({**body, "hash": _line_hash(body)}, sort_keys=True)
                handle.write(line.encode("utf-8") + b"\n")
                handle.flush()
                os.fsync(handle.fileno())
        except (OSError, TombstoneLogCorrupt) as exc:
            raise TombstoneWriteError(f"tombstone write failed: {exc}") from exc

    def read(self) -> list[Tombstone]:
        """Return every tombstone after verifying the whole chain."""
        try:
            with open(self.path, "rb") as handle:
                return _scan(handle)
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise TombstoneLogCorrupt(f"tombstone log unreadable: {exc}") from exc

    def head(self) -> dict[str, Any]:
        """Sequence number and hash of the newest line (for backup sidecars)."""
        entries = self.read()
        return {
            "seq": len(entries),
            "hash": entries[-1].hash if entries else GENESIS,
            "at": datetime.now(UTC).isoformat(),
        }


def tombstones_from_env(env: dict[str, str] | None = None) -> TombstoneSink:
    """The file log when ``WEAVES_TOMBSTONE_DIR`` is set, else a no-op sink."""
    directory = (env if env is not None else os.environ).get("WEAVES_TOMBSTONE_DIR")
    if directory and directory.strip():
        return FileTombstoneLog(directory.strip())
    return NullTombstones()


def main(argv: list[str]) -> int:
    """``python -m modules.recovery.tombstones head --dir DIR`` prints the head."""
    if len(argv) == 3 and argv[0] == "head" and argv[1] == "--dir":
        print(json.dumps(FileTombstoneLog(argv[2]).head()))
        return 0
    sys.stderr.write("usage: tombstones head --dir DIR\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
