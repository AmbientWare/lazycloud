# Observability Package

Durable events, usage and accounting, metrics, log and event streams, telemetry
setup, and the evidence billing is built on.

No rate card lives here. A quote comes from a published rate row; the placement
the control plane recorded supplies the quantity floor rather than the rate. This
package applies one and holds none.

A window is billed `max(reserved, measured)` per resource, reached without ever
pairing two records. The reservation is the floor because capacity held is
capacity nobody else can schedule onto, and it rides on the duration record —
the one a metering window guarantees. The measured records add only what the
same window used above the same floor, so the two sum to the greater of them
whatever order they arrive in, and a missing measurement costs the burst rather
than the charge. Nothing joins on time, looks a sibling window up, or derives a
quantity from another record: a stale window can only ever be priced against the
capacity that window held.

The sentry's and gofer's own resident memory is inside the measured memory
figure, and the platform charges for it. It exists because the container runs,
it is small against a gibibyte-second, and netting it out would put a per-runtime
correction inside a price.

"This window had no measured record" is not an anomaly and raises no durable
event: every image build has none, and so does every container that stayed under
its reservation. The `basis` column on each segment answers it instead, per
container and per window, at the cost of one string.

Pricing runs where metering commits. `MeteredUsagePricer` is built on the
caller's session so a priced segment lands in the transaction that wrote the
record it prices—a crash between the two loses money or bills it twice, and the
usage row alone cannot say which happened.

Metering is never refused. A window no published rate covers still commits its
usage record, writes no ledger row, invents no zero, and leaves a durable
`billing.span.unpriced` error naming the dimension, the gap and the reason. That
event is cluster-scoped: a missing rate is this platform's defect, not something
to show the customer whose work it failed to price. An explicit rate of zero is
the opposite case: it writes its segments and its allowance increment, so a free
dimension reads as metered at $0.00 rather than as unmeasured. It owes the
payment provider nothing—a zero moves no meter total, and the $0.00 line a
customer reads comes from the metered price their subscription carries rather
than from the events against it—so turning a dimension on later is still one rate
row and nothing else.

A record the ledger has already priced keeps the cost it froze. Re-recording a
quantity under an id that was already priced leaves the segments, the allowance
and the outbox where they are and raises a durable
`billing.span.reprice_refused` error naming both figures; a correction is a new
record, never an edit to a frozen one.

SQL access stays repository-backed, hot streams use coordination primitives, and
optional exporters initialize lazily so an unconfigured one costs nothing. Apps,
SDK, worker and scheduler loops, and providers stay outside.

Accounting, ordering, cursor semantics, and workspace isolation are correctness
properties here rather than conveniences: a dropped or misattributed event is a
billing defect.

A broad exception handler on a capacity, enrollment, or billing path either
records a durable error event or re-raises—it never swallows. Wrap the recording
itself, so that failing to record can never replace the failure being recorded.
