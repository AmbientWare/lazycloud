# Lazy images

## Goal

A container starts on a host that has never seen its image without first
downloading the whole image. The host fetches only the bytes the container
reads, keeps them in a local cache, and later shares them with nearby hosts.
The same code runs locally (registry, Garage) and in prod (ECR, S3).

Today the agent runs `docker pull` and waits for every layer. The first
container on a fresh host spent 2.45 s of a 5.1 s first call pulling a plain
Python image; torch and CUDA images take tens of seconds.

## How beta9 does it

beta9 (`../beta9`, read-only) converts each image into an indexed archive
(CLIP) in object storage. Workers mount the image over FUSE and fetch file
contents on read. A cache shared by the workers of one region holds 4 MiB
content-addressed pages, picks the owner of each page by rendezvous hashing,
prefetches and refills recently used images. beta9 runs containers without
Docker, so the FUSE mount is simply the root filesystem.

We keep Docker. The difference is only how the files reach the container: a
containerd snapshotter that answers Docker's request for each layer with our
lazily filled filesystem. stargz, SOCI and nydus plug into Docker the same
way, each with its own format; we use ours.

## Design

**Format.** Each OCI layer, after a build pushes it, becomes:

- an index: the layer's file tree with each entry's metadata (path, type,
  mode, owner, times, link target, xattrs, whiteouts) and, for regular files,
  the ordered list of chunks holding its bytes;
- content-defined chunks, named by their SHA-256, compressed, stored once
  however many layers or images contain them.

The index is keyed by the layer's uncompressed digest (`diff_id`), so a base
layer shared by many images is indexed once. Reuse the disk engine's
content-defined chunker (`internal/diskengine/chunker.go`) by moving it to the
shared package rather than writing a second one.

**Store.** Chunks and indexes live under one prefix of the platform object
store: S3 in prod, Garage locally. PostgreSQL records which layers of which
image are indexed; it is the authority for what exists and what is live.

**Publish.** The build already runs on a host and holds the layers it
pushed. After the push, the agent indexes each new layer, uploads missing
chunks and the index, and reports them; images records the layer indexes and
publishes the image only when every layer is indexed. A base layer already
indexed is skipped.

**Run.** A snapshotter process on each host serves containerd's snapshotter
API. When Docker pulls an image, it reports each indexed layer as already
present (a remote snapshot), so no layer blob downloads. Mounting a layer
starts a FUSE filesystem over its index; reads fetch chunks into a local
disk cache. The container's root is the usual overlay of those lower layers
plus a writable upper layer. The process runs as its own service and
outlives agent restarts and updates, so running containers keep their files.

**Access.** A host may read only the chunks of images it was told to run.
The server issues those reads (signed URLs or an equivalent scoped grant)
with the start command, never a credential for the whole store. This holds
for connected-account and self-hosted hosts too.

**Cache.** Each host keeps a bounded chunk cache on local disk with LRU
eviction; chunks in use by running containers stay. The peer cache, where
hosts of a region serve each other's chunks, is a later decision.

**Garbage.** Chunks and indexes that no live image references are swept, the
way the disk engine collects unreachable objects, by reading live
references, not history.

## Open questions the spike answers

1. Does Docker 29 with the containerd image store use a proxy snapshotter
   for `docker pull` and `docker run`, skipping layer downloads for layers the
   snapshotter reports as remote? Which daemon and containerd settings?
2. Does gVisor (`runsc`, its containerd shim) run a container whose lower
   layers are FUSE mounts, with the overlay and file reads behaving?
3. Does a FUSE mount held by a separate service survive an agent restart
   with the container still reading files?
4. What does a cold start on a fresh host gain, measured with an existing
   remote snapshotter (stargz) on a converted image, as an upper bound before
   we build ours?

A no on 1 or 2 stops the plan and comes back to the user with the options.

## Packets

See tasks/perf/README.md for branches, waves and the gate.

- **spike**: the four answers above, on a local machine and on one EC2 host
  from the node image. Scratch code only.
- **format**: `internal/imagefs`, the index encoding and reading, the moved
  chunker, chunk naming and compression, the object store client for chunks
  and indexes. A library with no database access; agent and snapshotter
  import it.
- **publish**: migration 0003 (indexed layers per image), images' publish
  rule, the build path in the agent that indexes and uploads, and the host
  protocol report.
- **snapshotter**: the host service (a `cmd/agent` subcommand run as its own
  unit), the containerd snapshotter API, the FUSE filesystem, the chunk cache
  and its bounds, node image and `deploy/local/host-setup.sh` configuration.
- **grants**: the scoped read path from server to snapshotter and its
  tests for tenant isolation.
- **acceptance**: before and after on a fresh EC2 host for a plain Python
  image and a torch image, warm-host cold starts unchanged or better, and
  the sweep of everything the old full pull did.
