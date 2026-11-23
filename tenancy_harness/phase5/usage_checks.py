"""Slice 5.3 control T5-I03: the usage ledger equals the run rows.

The fake model reports tokens, and a worker killed during a reservation
reconciles. Skeleton from slice 5.0. Slice 5.3 fills this in and moves
``T5-I03`` from ``planned_controls`` to ``required_controls`` in ``pair.json``.
Until then nothing imports or executes this module.
"""

CONTROL_ID = "T5-I03"
SLICE = "5.3"
