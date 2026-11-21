"""Pure, shared occurrence math for calendar-style workflow schedules.

Instants are always timezone-aware UTC; wall-clock rules are evaluated in an
explicit IANA zone. Records without a zone are legacy and read as Asia/Kolkata.

DST rules (documented contract, relied on by the API, studio tick and tests):

* A wall time that does not exist (spring-forward gap) runs at the first valid
  instant after the gap, that is the transition instant (02:30 -> 03:00).
* A wall time that occurs twice (fall-back overlap) runs once, at the first
  (earlier-offset) occurrence; the repeated wall time never fires again.
* ``hourly`` rules fire at every real instant whose local minute is zero, so a
  repeated wall hour yields two distinct instants and a skipped hour none.
* ``interval_seconds`` schedules are elapsed-time based and timezone independent;
  they never call this module.
* Occurrences returned are strictly increasing and strictly after ``after_utc``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from contracts.v1.timezones import validate_timezone_name

LEGACY_TIMEZONE = "Asia/Kolkata"
_LOOKAHEAD_DAYS = 22
_QUARTER_HOUR = timedelta(minutes=15)


class CalendarRule(Protocol):
    """The calendar fields the occurrence math reads."""

    @property
    def freq(self) -> str:
        """hourly, daily or weekly."""
        ...

    @property
    def days(self) -> list[int]:
        """Allowed weekdays, Monday is 0."""
        ...

    @property
    def time(self) -> str:
        """Local wall-clock time as HH:MM."""
        ...


def effective_timezone(name: str | None) -> str:
    """Legacy records without a zone behave as Asia/Kolkata."""
    return name if name else LEGACY_TIMEZONE


def resolve_zone(name: str | None) -> ZoneInfo:
    """Load a record's zone, treating a missing name as the legacy zone."""
    return ZoneInfo(validate_timezone_name(effective_timezone(name)))


def to_utc(value: datetime) -> datetime:
    """Normalize any aware datetime to UTC; naive values are rejected."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("schedule instants must include a timezone")
    return value.astimezone(UTC)


def _gap_end(wall: datetime, zone: ZoneInfo) -> datetime:
    """Transition instant (UTC) that ends the gap containing ``wall``."""
    low = wall.replace(tzinfo=zone, fold=1).astimezone(UTC)
    high = wall.replace(tzinfo=zone, fold=0).astimezone(UTC)
    if low > high:
        low, high = high, low
    before = low.astimezone(zone).utcoffset()
    while high - low > timedelta(seconds=1):
        mid = low + (high - low) / 2
        mid = mid.replace(microsecond=0)
        if mid.astimezone(zone).utcoffset() == before:
            low = mid
        else:
            high = mid
    return high


def wall_to_utc(wall: datetime, zone: ZoneInfo) -> datetime:
    """Resolve a naive wall time to UTC using the documented gap/overlap rules."""
    first = wall.replace(tzinfo=zone, fold=0)
    instant = first.astimezone(UTC)
    if instant.astimezone(zone).replace(tzinfo=None) != wall:
        return _gap_end(wall, zone)
    return instant


def _hourly(after: datetime, zone: ZoneInfo, count: int) -> list[datetime]:
    """Every real instant at local minute zero (offsets are quarter-hour aligned)."""
    step = int(_QUARTER_HOUR.total_seconds())
    cursor = datetime.fromtimestamp((int(after.timestamp()) // step + 1) * step, UTC)
    found: list[datetime] = []
    for _ in range(count * 4 + 24 * 4 * _LOOKAHEAD_DAYS):
        if cursor.astimezone(zone).minute == 0:
            found.append(cursor)
            if len(found) == count:
                break
        cursor += _QUARTER_HOUR
    return found


def _wall_clock(
    rule: CalendarRule, after: datetime, zone: ZoneInfo, count: int
) -> list[datetime]:
    hour, minute = map(int, rule.time.split(":"))
    start: date = after.astimezone(zone).date()
    found: list[datetime] = []
    for offset in range(_LOOKAHEAD_DAYS):
        day = start + timedelta(days=offset)
        if day.weekday() not in rule.days:
            continue
        instant = wall_to_utc(
            datetime(day.year, day.month, day.day, hour, minute), zone
        )
        if instant > after and (not found or instant > found[-1]):
            found.append(instant)
        if len(found) == count:
            break
    return found


def next_occurrences(
    rule: CalendarRule, after_utc: datetime, tz: str | None, count: int = 3
) -> list[datetime]:
    """Up to ``count`` UTC occurrences strictly after ``after_utc`` in zone ``tz``."""
    if any(day < 0 or day > 6 for day in rule.days):
        raise ValueError("Weekdays must be between Monday and Sunday")
    after, zone = to_utc(after_utc), resolve_zone(tz)
    if rule.freq == "hourly":
        return _hourly(after, zone, count)
    return _wall_clock(rule, after, zone, count)


def next_occurrence(
    rule: CalendarRule, after_utc: datetime, tz: str | None
) -> datetime | None:
    """The first occurrence strictly after ``after_utc``, or None if none exists."""
    found = next_occurrences(rule, after_utc, tz, 1)
    return found[0] if found else None


def organization_default_timezone(settings: object, org_id: str) -> str:
    """Organization zone from its settings record; Asia/Kolkata when absent."""
    getter = getattr(settings, "get_scoped", None)
    if getter is None:
        return LEGACY_TIMEZONE
    try:
        return effective_timezone(getter(org_id, org_id).timezone)
    except KeyError:
        return LEGACY_TIMEZONE


def calendar_identity(calendar: CalendarRule) -> tuple[object, ...]:
    """Comparable shape of a calendar, for detecting explicit time-of-day edits."""
    return (calendar.freq, tuple(sorted(calendar.days)), calendar.time)
