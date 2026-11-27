"""Which reaction names are feedback.

Slack names the thumbs-up emoji ``+1`` (alias ``thumbsup``) and the thumbs-down
emoji ``-1`` (alias ``thumbsdown``). A skin tone arrives as a suffix of the same
event field, for example ``+1::skin-tone-3`` (Slack's emoji naming, tones 2 to 6).
The suffix is stripped; a name with any other suffix, or any other emoji, is not
feedback. Removals are not read (no ``reaction_removed`` subscription).
"""

from __future__ import annotations

import re

_NAME = re.compile(r"(?P<name>\+1|-1|thumbsup|thumbsdown)(::skin-tone-[2-6])?")
_VALUE = {"+1": 1, "thumbsup": 1, "-1": -1, "thumbsdown": -1}


def feedback_value(reaction: str | None) -> int | None:
    """``1`` for a thumbs up, ``-1`` for a thumbs down, else ``None``."""
    if reaction is None:
        return None
    found = _NAME.fullmatch(reaction)
    return _VALUE[found.group("name")] if found else None
