# Images package

Container image planning, durable records and events, lifecycle decisions, build
execution and publication control, and cleanup, all behind scheduler, container,
storage, and secret protocols.

Apps, SDK, provider adapters, and scheduler and worker loops stay outside.
Managed-runtime catalogs hold one content-addressed artifact per supported Python
minor version and keep managed code separate from the dependencies it falls back
on.

Builds handle credentials, run long, and can fail halfway. Make durability,
concurrency, terminal failure, publication, and cleanup explicit: a build that
dies must not leave a half-published image that later loads as though it were
whole.

New build requests are preemptible. Dispatch retries preserve the submitted
request's recorded choice. A running build whose worker stops reporting becomes
failed, queues cleanup and requires a fresh submission to retry; it does not
restart automatically. Archive publication remains fenced by build ownership.

A build is a container the platform placed, and is admitted, recorded, metered
and billed as one. It carries the build's own id as its container id, so the
durable `containers` row, the `container_billing_shapes` placement written
against it, and the usage the worker reports all name one thing; a separate id
is one the ledger cannot resolve, and no row at all is compute nothing can
price. A build that fails held the capacity it was given for as long as it ran
and is billed for it.
