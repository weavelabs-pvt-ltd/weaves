"""Slice 5.1 control T5-I01: the API refuses the (cap+1)th task while the worker runs.

Organization B stays unaffected and durable row and model-call counts hold.
Skeleton from slice 5.0. Slice 5.1 fills this in and moves ``T5-I01`` from
``planned_controls`` to ``required_controls`` in ``pair.json``. Until then
nothing imports or executes this module.
"""

CONTROL_ID = "T5-I01"
SLICE = "5.1"
