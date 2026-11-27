"""Phase 7 (Slack bot) harness checks, declared planned in ``pair.json``.

Each slice owns one module here and one control id. ``T7-S00`` (slice S0) proves
the fake Slack server is reachable from the stack and speaks the signing
protocol. It is planned, not required: it is listed here only (the private-beta gate
requires ``pair.json`` ``planned_controls`` to stay empty), and the runner
ignores it until a later slice moves the id into ``required_controls``.

``T7-I01`` (install, slice S1), ``T7-I02`` (signed events, slice S2) and
``T7-I03`` (uninstall, revocation, quarantine and purge, slice S7) are required
controls, and ``T7-I04`` (the Ask flow, slice S4, ``slack_ask_checks``) and
``T7-I05`` (reaction feedback, slice S4c, ``slack_feedback_checks``) and ``T7-I06``
(Stop and Try again, slice S4b, ``slack_press_checks``, helpers in
``slack_press_support``) too: they
run in the seed container after ``T6-I03``, against the real API and worker with
the fake Slack server (``slack_install_checks``, ``slack_event_checks``,
``slack_lifecycle_checks``, shared helpers in ``slack_support``).

``T7-L01`` (``slack_live_checks``) is the opt-in live control: a real Ask and a
real Stop through the budget relay. It is not in ``pair.json`` and never runs in CI;
only ``--live-anthropic-env <file> --live-suite slack`` starts it.
"""

# control id -> (owning slice, module in this package); an id leaves this table
# when its slice moves it into ``required_controls``.
PLANNED = {
    "T7-S00": ("S0", "slack_selftest"),
}
