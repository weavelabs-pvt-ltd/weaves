"""Phase 7 (Slack bot) harness checks, declared planned in ``pair.json``.

Each slice owns one module here and one control id. ``T7-S00`` (slice S0) proves
the fake Slack server is reachable from the stack and speaks the signing
protocol. It is planned, not required: it is listed here only (the private-beta gate
requires ``pair.json`` ``planned_controls`` to stay empty), and the runner
ignores it until a later slice moves the id into ``required_controls``.
"""

# control id -> (owning slice, module in this package); an id leaves this table
# when its slice moves it into ``required_controls``.
PLANNED = {
    "T7-S00": ("S0", "slack_selftest"),
}
