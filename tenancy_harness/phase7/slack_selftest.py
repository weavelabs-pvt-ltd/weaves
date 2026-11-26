"""Control T7-S00: a signed url_verification round trip through the fake.

Needs only an HTTP client for the fake's private control endpoints. The stub is
the fake's own ``/slack/_stub/events``, which verifies the signature with the
product's ``verify_slack_request``. The check also proves the negative: a forged
signature is refused and no challenge is echoed.
"""

import httpx

CONTROL_ID = "T7-S00"
SLICE = "S0"
STUB_PATH = "/slack/_stub/events"


def run(control: httpx.Client, stub_base: str) -> None:
    """Raise ``AssertionError`` unless the signed round trip works both ways."""
    control.post("/slack/_control/reset").raise_for_status()
    url = stub_base.rstrip("/") + STUB_PATH
    payload = {"type": "url_verification", "challenge": "t7-s00-challenge"}
    try:
        good = _deliver(control, url, payload)
        assert good["delivered"] is True, "signed url_verification was refused"
        assert good["challenge_echoed"] is True, "challenge was not echoed"
        control.post(
            "/slack/_control/configure", json={"delivery_fault": "bad_signature"}
        ).raise_for_status()
        bad = _deliver(control, url, payload)
        assert bad["delivered"] is False, "a forged signature was accepted"
        assert bad["challenge_echoed"] is None, "a forged request got the challenge"
    finally:
        control.post("/slack/_control/reset").raise_for_status()


def _deliver(control: httpx.Client, url: str, payload: dict) -> dict:
    response = control.post(
        "/slack/_control/deliver_event", json={"url": url, "payload": payload}
    )
    response.raise_for_status()
    return response.json()
