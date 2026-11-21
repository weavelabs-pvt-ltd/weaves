"""Occurrence math: DST rules, legacy equivalence and ordering invariants."""

import json
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from contracts.v1.workflow_studio import Calendar
from modules.workflows.schedule_time import (
    effective_timezone,
    next_occurrence,
    next_occurrences,
    organization_default_timezone,
    resolve_zone,
    to_utc,
)

GOLDEN = Path(__file__).with_name("schedule_legacy_golden.json")
EVERY_DAY = [0, 1, 2, 3, 4, 5, 6]


def utc(*parts: int) -> datetime:
    return datetime(*parts, tzinfo=UTC)


def daily(time: str) -> Calendar:
    return Calendar(freq="daily", days=EVERY_DAY, time=time)


def test_legacy_records_match_values_captured_before_the_change():
    cases = json.loads(GOLDEN.read_text())
    assert len(cases) >= 150
    for case in cases:
        rule = Calendar.model_validate(case["calendar"])
        now = datetime.fromisoformat(case["now"])
        found = next_occurrences(rule, now, None)
        assert [item.isoformat() for item in found] == case["next"]
        assert all(item.utcoffset() == timedelta(0) for item in found)


def test_missing_zone_reads_as_asia_kolkata():
    assert effective_timezone(None) == "Asia/Kolkata"
    assert resolve_zone(None).key == "Asia/Kolkata"
    assert next_occurrence(daily("09:00"), utc(2026, 1, 1), None) == utc(
        2026, 1, 1, 3, 30
    )


def test_new_york_spring_forward_gap_runs_at_first_valid_instant():
    after = utc(2026, 3, 7, 8)
    first = next_occurrence(daily("02:30"), after, "America/New_York")
    assert first == utc(2026, 3, 8, 7, 0)
    local = first.astimezone(ZoneInfo("America/New_York"))
    assert (local.hour, local.minute) == (3, 0)
    assert next_occurrence(daily("02:30"), first, "America/New_York") == utc(
        2026, 3, 9, 6, 30
    )


def test_new_york_fall_back_overlap_runs_once_at_first_occurrence():
    zone = "America/New_York"
    after = utc(2026, 10, 31, 12)
    first = next_occurrence(daily("01:30"), after, zone)
    assert first == utc(2026, 11, 1, 5, 30)
    second = next_occurrence(daily("01:30"), first, zone)
    assert second == utc(2026, 11, 2, 6, 30)
    assert utc(2026, 11, 1, 6, 30) not in next_occurrences(
        daily("01:30"), after, zone, 5
    )


def test_hourly_fires_every_real_hour_through_both_transitions():
    zone = "America/New_York"
    assert next_occurrences(
        Calendar(freq="hourly"), utc(2026, 11, 1, 4, 30), zone, 3
    ) == [utc(2026, 11, 1, 5), utc(2026, 11, 1, 6), utc(2026, 11, 1, 7)]
    spring = next_occurrences(Calendar(freq="hourly"), utc(2026, 3, 8, 5, 30), zone, 3)
    local = [item.astimezone(ZoneInfo(zone)).strftime("%H:%M") for item in spring]
    assert local == ["01:00", "03:00", "04:00"]


def test_europe_london_gap_and_overlap():
    zone = "Europe/London"
    gap = next_occurrence(daily("01:30"), utc(2026, 3, 28, 12), zone)
    assert gap == utc(2026, 3, 29, 1, 0)
    overlap = next_occurrence(daily("01:30"), utc(2026, 10, 24, 12), zone)
    assert overlap == utc(2026, 10, 25, 0, 30)
    assert next_occurrence(daily("01:30"), overlap, zone) == utc(2026, 10, 26, 1, 30)


def test_lord_howe_thirty_minute_transition():
    zone = "Australia/Lord_Howe"
    gap = next_occurrence(daily("02:15"), utc(2026, 10, 3, 0), zone)
    assert gap == utc(2026, 10, 3, 15, 30)  # 02:30 LHDT, the end of the gap
    before = next_occurrence(daily("02:15"), utc(2026, 10, 2, 0), zone)
    assert before == utc(2026, 10, 2, 15, 45)  # 02:15 at +10:30
    overlap = next_occurrence(daily("01:45"), utc(2026, 4, 3, 20), zone)
    assert overlap == utc(2026, 4, 4, 14, 45)  # first 01:45 at +11:00
    assert next_occurrence(daily("01:45"), overlap, zone) == utc(2026, 4, 5, 15, 15)


def test_kolkata_utc_and_kiritimati_have_fixed_offsets():
    now = utc(2026, 6, 1, 0)
    assert next_occurrence(daily("09:00"), now, "Asia/Kolkata") == utc(
        2026, 6, 1, 3, 30
    )
    assert next_occurrence(daily("09:00"), now, "UTC") == utc(2026, 6, 1, 9)
    assert next_occurrence(daily("09:00"), now, "Pacific/Kiritimati") == utc(
        2026, 6, 1, 19
    )


def test_leap_day_and_month_ends():
    rule = Calendar(freq="daily", days=EVERY_DAY, time="00:00")
    assert next_occurrence(rule, utc(2028, 2, 28, 12), "UTC") == utc(2028, 2, 29)
    assert next_occurrence(rule, utc(2028, 2, 29), "UTC") == utc(2028, 3, 1)
    assert next_occurrence(rule, utc(2026, 12, 31, 23, 59), "UTC") == utc(2027, 1, 1)
    assert next_occurrence(rule, utc(2026, 2, 28, 0, 1), "UTC") == utc(2026, 3, 1)
    weekly = Calendar(freq="weekly", days=[4], time="18:00")
    dates = next_occurrences(weekly, utc(2026, 1, 1), "America/Los_Angeles")
    assert all(
        d.astimezone(ZoneInfo("America/Los_Angeles")).weekday() == 4 for d in dates
    )


@pytest.mark.parametrize(
    "zone",
    [
        "America/New_York",
        "Europe/London",
        "Australia/Lord_Howe",
        "Asia/Kolkata",
        "UTC",
        "Pacific/Kiritimati",
        "Pacific/Apia",
    ],
)
@pytest.mark.parametrize("time", ["00:30", "01:30", "02:30", "03:00", "23:59"])
def test_consecutive_occurrences_strictly_increase_and_never_repeat(zone, time):
    rule = daily(time)
    cursor, seen = utc(2026, 1, 1), []
    for _ in range(420):
        following = next_occurrence(rule, cursor, zone)
        assert following is not None and following > cursor
        seen.append(following)
        cursor = following
    assert len(set(seen)) == len(seen)
    days = [item.astimezone(ZoneInfo(zone)).date() for item in seen]
    assert len(set(days)) >= len(days) - 1  # at most one run per local date
    assert all(
        timedelta(hours=1) < b - a < timedelta(hours=49) for a, b in pairwise(seen)
    )


@pytest.mark.parametrize("zone", ["America/New_York", "Australia/Lord_Howe"])
def test_hourly_occurrences_strictly_increase(zone):
    cursor, seen = utc(2026, 3, 1), []
    for _ in range(24 * 280):
        cursor = next_occurrence(Calendar(freq="hourly"), cursor, zone)
        assert cursor is not None
        seen.append(cursor)
    assert seen == sorted(set(seen))


def test_rejects_naive_instants_and_bad_weekdays():
    with pytest.raises(ValueError, match="timezone"):
        to_utc(datetime(2026, 1, 1))
    rule = Calendar.model_construct(freq="daily", days=[9], time="09:00")
    with pytest.raises(ValueError, match="Weekdays"):
        next_occurrences(rule, utc(2026, 1, 1), None)
    with pytest.raises(ValueError, match="IANA"):
        resolve_zone("Mars/Olympus")


def test_organization_default_uses_settings_or_falls_back():
    class Settings:
        def get_scoped(self, record_id: str, org_id: str):
            if org_id != "org-a":
                raise KeyError(org_id)
            return type("Row", (), {"timezone": "America/New_York"})()

    assert organization_default_timezone(Settings(), "org-a") == "America/New_York"
    assert organization_default_timezone(Settings(), "org-b") == "Asia/Kolkata"
    assert organization_default_timezone(None, "org-a") == "Asia/Kolkata"
