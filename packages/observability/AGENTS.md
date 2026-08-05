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
