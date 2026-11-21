"""A New York organization with zoned and legacy-style schedules (control T4-I03)."""

import os

import psycopg

from tenancy_harness.client import call
from tenancy_harness.resources import member, resources, tenant

ZONE = "America/New_York"
EVERY_DAY = [0, 1, 2, 3, 4, 5, 6]


def _publish(t, name):
    """Publish a calendar workflow through the real studio route (org zone applies)."""
    plan = {
        "steps": [
            {"id": "one", "type": "agent", "a": [t["agent"]["id"]], "t": "Summarize"}
        ],
        "sched": False,
        "calendar": {"freq": "daily", "days": EVERY_DAY, "time": "09:00"},
    }
    body = {"name": name, "description": t["marker"], "plan": plan}
    published = call("POST", "/workflow-studio/publish", t["admin"], body)
    return published["definition"]["workflow_id"]


def _strip_zone(collection, record_id):
    """Rewrite one stored payload the way the previous release wrote it."""
    with psycopg.connect(os.environ["DATABASE_URL"]) as connection:
        changed = connection.execute(
            "UPDATE product_records SET payload = payload - 'timezone' "
            "WHERE collection = %s AND record_id = %s",
            (collection, record_id),
        ).rowcount
    if changed != 1:
        raise AssertionError("Legacy-style record was not found")


def seed_schedule_timezones(fixtures):
    """Create the zoned organization and strip the zone from one schedule/calendar."""
    t = tenant("zones", timezone=ZONE)
    t["user"] = member(t, "user")
    resources(t)
    default_calendar = _publish(t, t["marker"] + " calendar")
    legacy_calendar = _publish(t, t["marker"] + " legacy calendar")
    _strip_zone("canvas_settings", legacy_calendar)
    legacy = call(
        "POST",
        "/workflows/" + t["workflow"]["definition"]["workflow_id"] + "/schedules",
        t["admin"],
        {
            "name": t["marker"] + " legacy",
            "task_instructions": "legacy",
            "interval_seconds": 86400,
        },
    )
    _strip_zone("workflow_schedules", legacy["schedule_id"])
    return {
        "tenant": t,
        "zone": ZONE,
        "legacy_schedule_id": legacy["schedule_id"],
        "default_calendar_id": default_calendar,
        "legacy_calendar_id": legacy_calendar,
    }
