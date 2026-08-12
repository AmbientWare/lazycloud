# Observability Package

Durable events, usage and accounting, metrics, log and event streams, telemetry
setup, and the evidence billing is built on.

SQL access stays repository-backed, hot streams use coordination primitives, and
optional exporters initialize lazily so an unconfigured one costs nothing. Apps,
SDK, worker and scheduler loops, and providers stay outside.

Accounting, ordering, cursor semantics, and workspace isolation are correctness
properties here rather than conveniences: a dropped or misattributed event is a
billing defect.

A broad exception handler on a capacity, enrollment, or billing path either
records a durable error event or re-raises—it never swallows. Wrap the recording
itself, so that failing to record can never replace the failure being recorded.

## Where The Rates Come From

Two catalogs, derived differently on purpose, both in `observability.billing` and
both dated by `_PRICES_EFFECTIVE`.

**Sell prices** — what compute on this platform's own fleet costs — are
`max(competitor_anchor x 0.95, aws_cost x 1.5)`. The datacenter parts price off
spot and are preemptible because of it; the G-family parts stay on-demand, where
spot saves little and capacity is easier to hold.

**Management fees** — what compute on a customer's own cloud account costs — are
8% of the AWS `us-east-1` on-demand price, and nothing else. Never derive one
from the other: the sell table carries no on-demand figure and no consistent
multiple of one.

A GPU fee is 8% of that model's on-demand price per GPU-second, taken from the
smallest instance offering it and **net of the vCPU and memory that instance also
carries**, because those bill under their own managed rates. A model nobody can
net that way does not belong in the fee table, and therefore not in
`SUPPORTED_GPU_TYPES` either — the two are one list, so a card this platform
cannot price is a card it does not offer.

The two managed rates are solved from a pair of instances that differ only in
memory, so the split between vCPU and memory is observed rather than assumed:

    c5.xlarge   4 vCPU,  8 GiB, $0.170/hour
    r5.xlarge   4 vCPU, 32 GiB, $0.252/hour

    memory = (0.252 - 0.170) / (32 - 8)      = $0.003417 per GiB-hour
    vCPU   = (0.170 - 8 x 0.003417) / 4      = $0.035667 per vCPU-hour

    8% of vCPU   -> 0.035667 x 0.08 / 3600 x 1e9 =  793 nanos per vCPU-second
    8% of memory -> 0.003417 x 0.08 / 3600 x 1e9 =   76 nanos per GiB-second

Every GPU fee follows the same last step from its netted hourly figure, which is
recorded per model beside the table. Changing a rate means redoing this
arithmetic and moving `_PRICES_EFFECTIVE` forward — a rate change applies to
usage after its effective date and never re-prices a day already billed.
