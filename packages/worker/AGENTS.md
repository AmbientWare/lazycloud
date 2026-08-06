# Worker Package

Container execution: sequencing, runtime configuration, networking, mounts,
metrics and OOM handling, events, supervision, checkpoints, and finalization—all
behind narrow protocols.

Process entrypoints, API and SDK code, app state, and broad composition stay
outside. Worker-repository payloads live here with the worker, and the client
raises HTTP errors rather than returning soft results.

Every container runs under gVisor. That is not a default to be overridden: an
unspecified runtime resolves to `runsc`, no worker advertises anything else, and
asking for `runc` is refused by name rather than left to fail as a request that
provisions capacity and never places. Namespaces and cgroups are not the boundary
this package needs when the code inside them is a stranger's and the credentials
outside them are the platform's.

The sandbox couples three pins that would otherwise drift apart: the gVisor
release, the NVIDIA driver in the node image, and the GPU AMI. nvproxy proxies
only driver ABIs it was built against, so changing any one alone produces a fleet
whose GPU containers cannot start. `docker/Dockerfile.worker` and
`deploy/ami/bake.py` name the pair, and the bake refuses to register an image
whose installed driver is not one the pinned gVisor knows.

A worker holds credentials, runs untrusted code, and can die at any point in a
long sequence. Every stage has to be explicit about what it acquired and what
releases it: image handling, credential scope, retries, event emission, terminal
state, and cleanup.
