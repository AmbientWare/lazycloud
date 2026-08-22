# Container worker app

Worker settings, dependency assembly, the process loop, and the container-service
HTTP adapter. Execution behavior belongs in `packages/worker`.

A worker runs outside the control plane's trust boundary, and its dependency
graph has to reflect that. It reaches control-plane state and credentials only
through the authenticated worker-repository API, never the database, a cache, a
queue, or a control-plane service directly, while keeping its own image, cache,
mounted-storage, filesystem, and direct-transfer data paths.

Validate connection details before registering, and treat a lost connection as a
failure to report rather than a reason to fall back to a local substitute. A
worker that keeps running against local state after losing the control plane is
a worker whose work no longer exists anywhere.
