# Worker Package

Own container execution sequencing, runtime configuration, networking, mounts,
metrics/OOM, events, supervision, checkpoints, and finalization behind narrow
protocols. Process entrypoints, API/SDK, app state, and broad composition remain
outside. Worker-repository payloads live in `worker.repository_payloads`; its
client raises HTTP errors instead of soft results. Preserve OCI, credentials,
retries, events, terminal state, and cleanup through a real worker/container
when startup, materialization, or repository contracts change.
