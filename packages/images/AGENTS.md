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

New build requests are preemptible. Confirmed provider interruption permits one
automatic restart when durable inputs remain available and the original
submission deadline has not passed. Build-command failure and an unexplained
progress timeout fail the build. Cancellation prevents another attempt.

A public build owns up to two durable execution attempts. Each attempt has its
own container row, billing placement, log offset and upload key. Credentials,
progress, results and publication require the current execution container.
Retired containers cannot write into the successor's attempt. Cleanup names the
retired container and waits for upload capabilities to expire before deleting
unpublished bytes; globally retained image archives survive attempt cleanup.
Each attempt is billed for the compute it used, including a failed attempt.
