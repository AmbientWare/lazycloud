# Worker package

Container execution: sequencing, runtime configuration, networking, mounts,
metrics and OOM handling, events, supervision, checkpoints, and finalization, all
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

Devices are half of it. The other half is the driver's userspace, and stripping
the hooks is exactly what leaves it behind, because the hook is what ordinarily
puts it there. A CUDA image links against `libcuda.so.1` and never ships it: the
driver has to match the kernel module on the node, so it can only come from the
node. This package mounts it instead, one bind per file into
`/usr/local/nvidia/lib64`, which `LD_LIBRARY_PATH` already names and names first.

Those files are read off the worker's own filesystem, not the node's. The agent
starts the worker container with `--gpus`, so the NVIDIA runtime has already put
the node's driver there and already settled whether this worker image can use it;
reading the node directly would reach past that answer to guess at it again.
Every name is kept, versioned file and soname symlink alike, because the file is
`libcuda.so.590.48.01` and the name every CUDA program asks for is
`libcuda.so.1`. Mounting the directory whole is the wrong shape: it is the
worker's `/usr/lib`, and it would stack the worker's glibc in front of the
workload's.

A worker that finds no driver refuses the container rather than allocating GPUs
it cannot make usable. This failed silently for as long as it existed. The
planner searched two directories the node image never created, matched neither,
returned no mounts, and every GPU workload started with its devices, no driver,
and a loader error that read as the author's bug.

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

Writable layers share a quota-enabled XFS backing image sized to the host's
available disk, capped at the worker's configured backing size. The host keeps
a free-space reserve outside that image. Container disk limits are ceilings,
not reservations; admission checks both the writable filesystem and the host
space backing its sparse image. Provider adapters do not choose this layout.

A metering window is claimed once and, if its write fails, offered again exactly
as it was claimed, with the same bounds and the same evidence. The platform
derives a usage record's identity from those bounds and prices the window against
the capacity it held, so re-sending an unchanged window is refused as a
duplicate, while widening it to reach the present asks for ground that may
already be priced under ids that cannot collide with the charge holding it.
Ground metered after the failure belongs to the window that follows, not to the
one being retried: evidence and window travel together, and a retry that carried
current samples against an earlier window would bill a burst that window never
saw.

Image builds own a cgroup for Buildah, registry pushes and image indexing. Each
child joins before exec; moving it afterwards can leave descendants outside the
limit. Index generation uses one invocation of the image-runtime binary, so the
build can kill and reap it without affecting mounted images. Independent CPU/RSS
sampling closes measured windows while delivery retries the same evidence. The
final window ends at the owned processes' stop time, which also travels with the
build result. A delayed upload or result retry must not extend that interval.

What a container reserved is not restated as a usage metric. The reservation
prices from the placement the control plane recorded, so a worker's own copy of
it is a label, and a second copy that decided nothing would still have to be
kept in step with the one that does.

Raw interface TX is telemetry. Billable internet egress counts routed IP bytes,
including retransmissions, on the container's owned host veth after forwarding
authorization. It excludes private destinations and service routes the provider
verified for the durable machine owner. An address family without that evidence
is unbilled. These counters never change firewall authorization, and cleanup
removes only the container's own rules. Usage still uses the worker's bounded
windows; a crash or unavailable final sample can lose unflushed evidence.

Execution creates an accounting cgroup before starting the runtime. The
runtime uses its child, so cumulative CPU counters survive guest exit and runtime
cleanup. CPU billing uses counter deltas. RSS billing integrates each observed
`anon + file_mapped` value over the interval until the next sample. The final
sample closes that interval before the accounting parent is removed. A missing
counter is incomplete evidence, never zero usage.

Billing enforcement uses the normal container stop path after recorded usage
exhausts the account balance. Workers send usage windows and terminal evidence;
they do not reserve money or renew financial execution permits. CPU and memory
cgroups remain the resource boundary. Builds register their process cancellation
with the same stop registry and retain their final usage window during cleanup.

A container that outgrows its reservation is stopped here, not by the kernel.
The worker holds the two readings the decision needs, its own memory pressure
and what each container currently uses, and the kernel is seconds away once a
machine is in full stall, so the decision is made where the readings are rather
than reported to something that would have to ask again. The scheduler learns
about it the same way it learns about any other stop. Nothing is requeued, and
nothing needs to be: a request stops being recoverable once a worker has taken
it, so a container that is running has no queued work waiting behind it. What
recovers is whatever recovers any container that died: the autoscaler for an
endpoint or a pod, the retry schedule for a function task.

The rule is the reservation, and it is the whole of what a reservation buys: the
container furthest above what it asked for goes, and one inside its request is
never chosen however large it is. That inverts `oom_badness`, which scores
resident size and reaches the biggest honest tenant first, which is why this
cannot be delegated to the kernel and then explained to the customer afterwards.

A worker whose cgroup carries no memory limit does not watch. Both deployments
give a worker a cgroup, since the agent passes `--cgroupns host` and Compose sets
`cgroup: host`, so having one proves nothing; what matters is whether it is
bounded. Unbounded, the pressure reading describes the whole machine rather than
this worker's share, and evicting on it stops this worker's containers because
something else on the host grew.

A container's cgroup nests inside the worker's, and the worker moves its own
processes into a leaf first. cgroup v2 refuses to enable a controller on a cgroup
that holds processes, so a worker sitting directly in its own cgroup can never
give its containers a limit there. The child is created and has no `memory.max`
at all. Nesting is what makes the slot a bound and the worker's `memory.pressure`
a signal about this worker rather than the machine, so neither half is optional.

None of that is visible without a real runsc and a real cgroup filesystem, and
all of it is visible immediately with one. The same is true of the eviction
threshold: a worker pinned at its memory limit with more than its own size
swapped out reads under 1.5% full stall, so the first threshold of twenty per
cent, chosen rather than measured, described a machine already dead and would
never have fired. When these values change, run the production path against the
worker image rather than trusting the unit suite: build the spec with
`build_base_oci_config` and `plan_oci_linux_resources`, run it under `runsc` in
a privileged container with `--cgroupns=host`, and read the cgroup back. Four
review rounds and a green suite each missed a different way this was inert; one
such run found the controller-delegation problem in a single attempt.

For the same reason every figure a ceiling is clamped against comes from that
cgroup rather than from `/proc/meminfo` or `os.cpu_count()`, neither of which
Docker namespaces. Several workers share a host, each started with its own
`--memory`, and a worker reading the machine would hand every container a
ceiling it cannot honour.
