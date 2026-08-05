# Worker Package

Container execution: sequencing, runtime configuration, networking, mounts,
metrics and OOM handling, events, supervision, checkpoints, and finalization—all
behind narrow protocols.

Process entrypoints, API and SDK code, app state, and broad composition stay
outside. Worker-repository payloads live here with the worker, and the client
raises HTTP errors rather than returning soft results.

A worker holds credentials, runs untrusted code, and can die at any point in a
long sequence. Every stage has to be explicit about what it acquired and what
releases it: image handling, credential scope, retries, event emission, terminal
state, and cleanup.
