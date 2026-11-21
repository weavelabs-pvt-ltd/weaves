"""IANA time zone names for schedules, validated with the standard library."""

from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AfterValidator

MAX_TIMEZONE_LENGTH = 64


def validate_timezone_name(name: str) -> str:
    """Return a valid IANA zone name or raise ValueError with a safe message."""
    clean = name.strip() if isinstance(name, str) else ""
    if not clean or len(clean) > MAX_TIMEZONE_LENGTH:
        raise ValueError(
            "timezone must be an IANA time zone name of 1 to 64 characters"
        )
    try:
        ZoneInfo(clean)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ValueError("timezone must be an IANA time zone name") from exc
    return clean


ScheduleTimezone = Annotated[str, AfterValidator(validate_timezone_name)]

__all__ = ["MAX_TIMEZONE_LENGTH", "ScheduleTimezone", "validate_timezone_name"]
