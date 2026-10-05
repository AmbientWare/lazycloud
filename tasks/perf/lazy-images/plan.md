# Lazy images

## Goal

A container starts on a host that has never seen its image without first
downloading the whole image. The host fetches only the bytes the container
reads, keeps them in a local cache, and later shares them with nearby hosts.
The same code runs locally (registry, Garage) and in prod (ECR, S3).

Today the agent runs `docker pull` and waits for every layer. The spike
(spike.md) measured a fresh host, median of three, under runsc:

| Image | Full pull today | Lazy (stargz stand-in) | Bytes read |
| --- | --- | --- | --- |
| python:3.12-slim | 2.79 s | 2.51 s | 11 of 50 MB |
| torch CPU | 25.15 s | 8.45 s | 105 of 728 MB |

The registry in those runs was local, so a remote store should widen the gap.

## How beta9 does it

beta9 (`../beta9`, read-only) converts each image into an indexed archive
(CLIP) in object storage. Workers mount the image over FUSE and fetch file
contents on read. A cache shared by the workers of one region holds 4 MiB
content-addressed pages, picks the owner of each page by rendezvous hashing,
prefetches and refills recently used images. beta9 runs containers without
Docker, so the FUSE mount is simply the root filesystem.

We keep Docker and plug in through containerd's snapshotter API, which the
spike proved under gVisor.

## Design

**Format.** Each OCI layer becomes two objects, keyed by the layer's
uncompressed digest (`diff_id`), so a base layer shared by many images is
stored once:

- `index`: the layer's file tree. Each entry has its path, type, mode, owner,
  times, link target, device numbers, xattrs, whiteout or opaque marker and,
  for a regular file, the offset and length of its bytes in `data`. A header
  carries the format version; a format change is a hard cut, never a second
  reader.
- `data`: every regular file's bytes, concatenated, compressed in independent
  frames of a fixed size (4 MiB uncompressed), with the frame table in the
  index so any byte range is read with one or a few HTTP range requests.

One object pair per layer keeps uploads, signing and deletes cheap, and the
4 MiB frame is the unit the host cache and the later peer cache hold.

**Store.** The pair lives under one prefix of the platform object store: S3
in prod, Garage locally. PostgreSQL records which layers are converted and
which image uses which layer; it is the authority for what exists and what
is live.

**Publish.** The build already runs on a host and holds the layers it
pushed. After the push, the agent converts each layer the store does not
already have, uploads the pair through URLs the server presigns, and reports
it. Images publishes an image only when every layer is converted. Every image
a host runs is converted; the spike showed an unconverted image on the lazy
driver is as slow as a full pull, so there is no unconverted path.

**Pull.** The agent pulls through containerd's API into Docker's namespace,
with the labels that let our snapshotter report each layer as already
present, then runs the container with Docker as today. This works on the
node image's Docker 25 and on Docker 29.9, whose own `docker pull` now always
downloads layers (moby#49784). The registry still serves manifests and
configs; no layer blob downloads.

**Mount.** A snapshotter service on each host serves containerd's snapshotter
API. Mounting a layer starts a FUSE filesystem over its index; reads fetch
frames into a local disk cache. The container's root is the usual overlay of
those lower layers plus a writable upper layer. Docker runs with the
containerd image store, our snapshotter as its storage driver and
`live-restore`.

**Lifetime.** A FUSE mount dies with the process that serves it: the spike
broke a running container by restarting that process. The service that
holds FUSE therefore never restarts while containers run. Agent updates
leave it alone; it changes only with the node image, which the fleet
replaces, or on a drained host. Its unit uses `KillMode=process`.

**Access.** A host reads only the layers of images it was told to run. The
server sends presigned GET URLs for each layer's `index` and `data` with the
start command, and fresh ones before they expire, the way `StorageGrant`
refreshes. Range requests work on presigned URLs in S3 and Garage alike. No
host, including a connected account's, holds a credential for the store.

**Cache.** Each host keeps a bounded frame cache on local disk with LRU
eviction; frames of layers mounted by running containers stay until those
mounts go. The peer cache, where hosts of a region serve each other's frames,
is decided from the acceptance numbers.

**Garbage.** Layer pairs no live image references are swept by reading live
references, not history.

## Local development

Docker uses one storage driver for every container. Making our snapshotter
the local Docker's driver changes the developer's whole Docker, and switching
on the containerd image store hides images pulled under overlay2 until it is
switched back. The snapshotter packet proposes the local setup and the
integrator asks the user before `deploy/local/host-setup.sh` changes it.

## Packets

See tasks/perf/README.md for branches, waves and the gate.

- **format**: `internal/imagefs`: index encoding, layer conversion from an
  OCI tar stream, frame compression, range reads. A library with no database
  access.
- **publish**: migration 0003, images' publish rule and live references, the
  build path that converts and uploads, presigned upload URLs, the host
  protocol report.
- **snapshotter**: the host service, the containerd snapshotter API, FUSE,
  the frame cache, the agent's containerd pull, node image and local
  configuration.
- **grants**: presigned read URLs per started container, their refresh, and
  the tenant isolation tests.
- **acceptance**: before and after on fresh EC2 hosts with S3, prod after
  Ship.
