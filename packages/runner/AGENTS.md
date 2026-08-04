# Runner Package

The small transport-oriented entrypoints that execute inside user containers.

It runs beside arbitrary user code, so its dependency surface is deliberately
minimal: shared contracts and environment helpers plus the user's own code—never
backend domains, apps, persistence clients, schedulers, workers, or providers.
Calls use the current public HTTP contracts.

Keep the module entrypoints importable and guard user imports, so a failure in
user code is reported as a user failure with its own framing rather than as a
runner crash.

Runner ships as a versioned content-addressed artifact mounted read-only by the
worker. The artifact and the worker image that carries it move together.
