"""Slice 5.4 control T5-I04: browser Home on a large seeded history stays bounded.

Request count, per-request SQL count and row count are constant. Skeleton from
slice 5.0. Slice 5.4 fills this in and moves ``T5-I04`` from
``planned_controls`` to ``required_controls`` in ``pair.json``. Until then
nothing imports or executes this module.
"""

CONTROL_ID = "T5-I04"
SLICE = "5.4"
