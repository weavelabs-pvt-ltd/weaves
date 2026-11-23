"""Slice 5.5 control T5-I05: a request id flows from API response to worker log.

No secret or prompt appears in any logged field. Skeleton from slice 5.0.
Slice 5.5 fills this in and moves ``T5-I05`` from ``planned_controls`` to
``required_controls`` in ``pair.json``. Until then nothing imports or executes
this module.
"""

CONTROL_ID = "T5-I05"
SLICE = "5.5"
