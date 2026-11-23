"""Slice 5.2 control T5-I02: a hot tenant cannot starve another.

Two native workers; organization A has a deep slow-fake-model backlog and
organization B submits one task that must start within the declared bound.
Skeleton from slice 5.0. Slice 5.2 fills this in and moves ``T5-I02`` from
``planned_controls`` to ``required_controls`` in ``pair.json``. Until then
nothing imports or executes this module.
"""

CONTROL_ID = "T5-I02"
SLICE = "5.2"
