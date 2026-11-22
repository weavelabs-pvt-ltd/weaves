"""Legacy IANA aliases reported by some browsers must validate."""

import pytest

from contracts.v1.timezones import validate_timezone_name


@pytest.mark.parametrize(
    "name", ["Asia/Kolkata", "Asia/Calcutta", "Asia/Katmandu", "America/Buenos_Aires"]
)
def test_canonical_and_legacy_zone_names_are_accepted(name: str) -> None:
    assert validate_timezone_name(name) == name


def test_unknown_zone_is_rejected() -> None:
    with pytest.raises(ValueError, match="IANA"):
        validate_timezone_name("Mars/Olympus")
