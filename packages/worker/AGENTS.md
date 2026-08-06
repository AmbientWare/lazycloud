# Worker Package

Container execution: sequencing, runtime configuration, networking, mounts,
metrics and OOM handling, events, supervision, checkpoints, and finalization—all
behind narrow protocols.

Process entrypoints, API and SDK code, app state, and broad composition stay
outside. Worker-repository payloads live here with the worker, and the client
raises HTTP errors rather than returning soft results.

Every container runs under gVisor. That is not a default to be overridden: an
unspecified runtime resolves to `runsc`, no worker advertises anything else, and
there is no longer a `runc` binary or command path to fall back to. Namespaces
and cgroups are not the boundary this package needs when the code inside them is
a stranger's and the credentials outside them are the platform's.

A request or persisted record naming `runc` resolves to `runsc` rather than being
refused. Refusing reads as the safer choice and is not: stubs and in-flight
containers written before the move still name it, and rejecting them stranded a
running container on a worker advertising only `runsc`, with no route left to
stop or inspect it. Honour the container, not the obsolete runtime.

gVisor decides a container wants a GPU by finding `/dev/nvidiactl` among
`linux.devices`, or by an nvidia hook it can read `NVIDIA_VISIBLE_DEVICES` from.
This package injects no hooks and strips the ones it finds, so the device nodes
are the only signal, and anything that clears them turns a GPU workload into one
that runs with `--nvproxy` set over a sandbox gVisor already decided needed no
GPU. It fails as a CUDA initialisation error naming neither GPUs nor devices.

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
