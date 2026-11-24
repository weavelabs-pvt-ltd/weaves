"""Phase 5 (limits, fair execution, usage ledger, bounded queries) harness checks.

Each slice 5.1 to 5.5 owns exactly one module here and one control id. Slice
5.1's ``T5-I01`` is already required. The others are declared ``planned`` in
``pair.json`` (listed in ``PLANNED``) and are not required, so the runner
ignores them until their slice moves the id into ``required_controls``. Slice
5.6 adds the load and noisy-neighbour modules.
"""

# control id -> (owning slice, module in this package); an id leaves this table
# when its slice moves it into ``required_controls`` (5.4: T5-I04).
PLANNED = {
    "T5-I02": ("5.2", "fair_claim_checks"),
    "T5-I03": ("5.3", "usage_checks"),
}
