"""Phase 7 (Slack bot) harness checks, declared planned in ``pair.json``.

Each slice owns one module here and one control id. ``T7-S00`` (slice S0) proves
the fake Slack server is reachable from the stack and speaks the signing
protocol. It is planned, not required: it is listed here only (the private-beta gate
requires ``pair.json`` ``planned_controls`` to stay empty), and the runner
ignores it until a later slice moves the id into ``required_controls``.

``T7-I01`` (install, slice S1), ``T7-I02`` (signed events, slice S2) and
``T7-I03`` (uninstall, revocation, quarantine and purge, slice S7) are required
controls: they run in the seed container after ``T6-I03``, against the real API
and worker with the fake Slack server (``slack_install_checks``,
``slack_event_checks``, ``slack_lifecycle_checks``, shared helpers in
``slack_support``).
"""

# control id -> (owning slice, module in this package); an id leaves this table
# when its slice moves it into ``required_controls``.
PLANNED = {
    "T7-S00": ("S0", "slack_selftest"),
}
