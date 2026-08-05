# Images Package

Container image planning, durable records and events, lifecycle decisions, build
execution and publication control, and cleanup—all behind scheduler, container,
storage, and secret protocols.

Apps, SDK, provider adapters, and scheduler and worker loops stay outside.
Managed-runtime catalogs hold one content-addressed artifact per supported Python
minor version and keep managed code separate from the dependencies it falls back
on.

Builds handle credentials, run long, and can fail halfway. Make durability,
concurrency, terminal failure, publication, and cleanup explicit: a build that
dies must not leave a half-published image that later loads as though it were
whole.
