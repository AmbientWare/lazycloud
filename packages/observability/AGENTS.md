# Observability Package

Own durable events, usage/accounting, metrics, log/event streams, telemetry
setup, and billing evidence. SQL access stays repository-backed, hot streams use
coordination primitives, and optional exporters initialize lazily. No apps, SDK,
worker/scheduler loops, or providers. Preserve accounting, ordering, cursor,
workspace isolation, and loss-risk behavior through the real repository and
API/stream.
