# Runner Package

Own small transport-oriented entrypoints executed inside user containers. Depend
only on shared contracts/environment helpers and user code—never backend
domains, apps, persistence clients, schedulers, workers, or providers. Calls use
current `/api/v1` and `/gateway` contracts. Keep `python -m runner.function`,
`runner.serve`, and `runner.taskqueue` importable and guard user imports through
`shared.env`.

Runner ships as a versioned content-addressed artifact mounted read-only by the
worker; changes require rebuilding the worker image before live acceptance.
Exercise affected entrypoints in a representative workload, including payload,
user-import failure, and result/error framing.
