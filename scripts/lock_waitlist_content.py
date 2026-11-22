"""Pin newly added waitlist question sets and consent texts in the lock file.

Run after adding ``contracts/waitlist/questions/v<N>.json`` or
``consent/v<N>.json``. It only ADDS entries: a published file that is already
locked is never re-hashed, so editing one still fails startup and the tests.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    """Add the missing entries and print what was pinned."""
    sys.path.insert(0, str(ROOT))
    from modules.waitlist.questions import (
        CONTENT_ROOT,
        LOCK_NAME,
        digest,
        published_files,
    )

    lock_path = CONTENT_ROOT / LOCK_NAME
    current = (
        json.loads(lock_path.read_text()).get("files", {}) if lock_path.exists() else {}
    )
    added = []
    for name, path in published_files(CONTENT_ROOT).items():
        if name not in current:
            current[name] = digest(path)
            added.append(name)
    text = json.dumps({"files": current}, indent=2, sort_keys=True)
    lock_path.write_text(text + "\n")
    print("pinned: " + (", ".join(added) if added else "nothing new"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
