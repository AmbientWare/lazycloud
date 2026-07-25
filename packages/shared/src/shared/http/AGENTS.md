# Shared HTTP Contracts

Own every public JSON payload consumed by API, SDK/CLI, runner, or web. Keep one
domain module and one precise `HttpModel` per wire payload; use `datetime`,
closed enums, `{data, next}` lists, base64 byte transport, and model validators
for wire invariants. Failures use HTTP statuses and `ErrorResponse`; deliberate
stream-event statuses remain domain data.

Contract changes update API routes/services, SDK/CLI, runner, `__all__`, and
consumed web Zod schemas together. Delete unconsumed models. Accept through one
representative producer/consumer request, preserving validation, authorization,
information loss, and error mapping; runner-facing changes also require a real
workload before live acceptance.
