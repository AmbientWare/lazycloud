# Images package

Container image planning, durable records and events, lifecycle decisions, build
execution and publication control, and cleanup, all behind scheduler, container,
storage, and secret protocols.

Apps, SDK, provider adapters, and scheduler and worker loops stay outside.

A project image (uv, Poetry, pyproject) installs dependencies only. The root
project is skipped (`--no-install-project`, `--no-root`) because its code arrives
through source sync and must not be baked into the image, where every edit would
change the image identity. Local dependencies named by the manifests are still
installed from the uploaded context. `IMAGE_BUILD_IDENTITY_CONTRACT_VERSION`
changes whenever what an image is built from changes, so archives built under
the old identity are never mistaken for the new one.

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
