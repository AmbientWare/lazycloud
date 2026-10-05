# Snapshotter

## Scope

The host service that gives Docker lazily read layers, and the agent's pull
through it. Owns:

- the service: a binary or `cmd/agent` subcommand run as its own systemd unit
  with `KillMode=process`, never restarted by agent updates, serving
  containerd's snapshotter API on a local socket;
- `internal/imagefs/snapshotter` (or the layout format settles): the
  snapshotter API, one FUSE filesystem per mounted layer over its index, the
  frame cache on local disk with a size bound and LRU eviction that keeps
  frames of mounted layers, cleanup of mounts no container uses;
- the agent's image pull (`imageCache.ensure` in `internal/agent/source.go`):
  pull through containerd's API into Docker's namespace with the labels that
  make the snapshotter report converted layers as present, then run with
  Docker as today;
- `deploy/ami/node-setup.sh`: Docker with the containerd image store, our
  snapshotter as storage driver, `live-restore`; containerd's proxy plugin
  entry; the unit. Keep the node image's Docker 25 unless the report shows a
  reason.
- `deploy/local/host-setup.sh`: propose the local setup in the report first
  (it changes the developer's whole Docker); change it only after the
  integrator confirms with the user.

Stay off images, the build path, `contracts/imagefs` and the server.

## Plan

- The snapshotter gets each layer's presigned URLs from the agent over the
  local interface grants and this packet agree in their first commits; until
  grants lands, read them from that interface fed by a test, never from a
  store credential.
- A read the store cannot serve returns an I/O error to the container,
  logged and counted, never an empty file. Retries are bounded.
- Concurrent reads of one frame fetch it once.

## Evidence to record

- On an EC2 host from a node image built with these settings: a container
  runs from a converted image with no layer blob downloaded, under runsc;
  `import torch` works; file contents match the original image.
- A running container keeps reading across an agent restart and update.
- The cache stays under its bound while two large images alternate.
- Cold start table against today's full pull for python and torch.

## Progress

Done on 2026-10-05 on `perf-lazy-snapshotter`.

- `contracts/imagefs/v1/source.proto` (commit 99558431): `LayerSources.Grant`
  on the snapshotter's socket. Of the grants for one diff_id the one that
  expires last is current; reads take it at each request; a lazy Prepare
  without an unexpired grant fails `FailedPrecondition` saying so; a call
  with any malformed grant records none. Both imagefs protos share one Go
  package, `internal/imagefs/imagefsproto` (was `indexproto`; buf needs one
  `go_package` per proto package). Client: `internal/imagefs/layersource`
  (`Dial`, `Grant`, `Socket`, `Snapshotter`, `LazyLabel`).
- `cmd/snapshotter` and `internal/imagefs/snapshotter`: containerd's overlay
  snapshotter on a shared metadata store, plus lazy layers. A Prepare whose
  labels carry `LazyLabel` fetches the layer's index, commits a snapshot
  holding only the index under the Prepare key with the target labels, and
  returns AlreadyExists, so containerd skips the download. A lazy layer is
  FUSE-mounted (go-fuse, read-only, inode tree built from the index,
  overlayfs whiteouts and `trusted.overlay.opaque`) while a snapshot that is
  not a lazy layer descends from it or a call holds it; one goroutine owns
  every mount and unmount, so a Prepare and the unmount of an unused layer
  never interleave; a busy unmount is kept and retried on the next Remove or
  Cleanup. Frames go to a disk cache under a byte bound, LRU, mounted
  layers' frames evicted last, emptied at start (the store is its source).
  Concurrent reads of a frame share one fetch; fetches are bounded (16) and
  retried at most 3 times; a read the store cannot serve is EIO, logged and
  counted (`lazycloud_snapshotter_read_failures_total`). Mounted layers up
  to `--fill-bytes` (256 MiB) are fetched whole in the background. Startup
  detaches mounts a previous process left and needs no store.
- `internal/agent`: `imageCache.ensureLazy` pulls through containerd into
  `moby` with every layer annotated lazy; the start path uses it. Platform
  images (builder, mount image) still `docker pull` and unpack on the
  snapshotter's overlay path. Preflight check `snapshotter` fails unless the
  socket is served and Docker's storage driver is `lazycloud`.
- `cmd/agent`: `install-service` (and `install-snapshotter`) copies the
  snapshotter from the release to `/usr/local/lib/lazycloud`, writes its
  unit (`Type=notify`, `KillMode=process`, before containerd and Docker),
  adds the containerd proxy plugin, sets `containerd-snapshotter`,
  `storage-driver: lazycloud` and `live-restore`, and restarts containerd
  and Docker only when those changed. A running snapshotter is never
  replaced. The bundle carries `lazycloud-snapshotter`.
- `deploy/ami/node-setup.sh` carries the same Docker and containerd
  settings, so the boot-time install restarts neither. Docker 25 stays.

## Evidence

EC2, us-east-2a, m7i.large from ami-0e3303f49502924b3 (Docker 25.0.16,
containerd 2.2.7, runsc 20260928), 60 GiB gp3. Layers converted with
`internal/imagefs` into a scratch S3 bucket and read through presigned GETs;
manifests and configs from a registry:3 on the host.

- `TestLazyPullDownloadsNoLayer` (`internal/agent`, runs on a snapshotter
  host): python:3.12-slim and pytorch/torchserve:0.12.0-cpu pull with no
  layer blob in the content store and none requested from the registry
  (only the config), then run under runsc; `import torch` prints 3.0.
- File trees match: every file and symlink with mode, owner, size and
  sha256, lazy under runsc against the original image unpacked by Docker
  under runc: python 4920 entries identical, torch 55001 identical.
- Owner tests, as root on the host and unprivileged locally:
  `TestLazyLayerServesItsFiles`, `TestOpaqueDirectoriesCarryTheOverlayMarker`,
  `TestPullingAnUngrantedLayerFails`, `TestUnservableReadsAreIOErrors`,
  `TestUnusedLayersAreUnmounted`, `TestContainersStackLazyLayers`,
  `TestConcurrentReadsFetchAFrameOnce`, `TestMountedLayersFillInTheBackground`,
  `TestFrameCacheStaysUnderItsBound`, `TestGrantsKeepTheLatestExpiry`,
  `TestSnapshotterSettingsKeepTheHostsConfiguration` (`cmd/agent`).

- Restarts: a runsc container hashing all 47017 files of the torch image,
  one every 4 ms from a cold cache, kept running through
  `systemctl restart containerd`, `systemctl restart docker` (live-restore)
  and a rerun of `install-snapshotter`, which left the snapshotter alone
  (NRestarts=0); it exited 0 with no read error. Restarting the snapshotter
  itself under a running container gave 2973 reads `ENOTCONN`, never other
  bytes. Docker starts with the snapshotter down and serves once it is up.
- Cache bound: with `--cache-bytes=268435456`, torchserve 0.12.0 and 0.11.0
  pulled and run alternately three times each: the cache held exactly
  268435456 bytes of frames after each run, 800 evictions, 1.03 GB fetched,
  0 read failures, every run passed; no layer stayed mounted afterwards.

Cold start, median of 3, each run with the image removed, the snapshotter
restarted (empty cache), page cache dropped; pull plus one
`docker run --runtime runsc`:

| Path | Image | Pull | Run | Total | Bytes read |
| --- | --- | --- | --- | --- | --- |
| overlay2 today, local registry | python:3.12-slim | 1.42 | 1.34 | 2.76 | 46 MB |
| overlay2 today, Docker Hub | python:3.12-slim | 2.03 | 1.57 | 3.60 | 46 MB |
| lazy, S3 | python:3.12-slim | 0.40 | 1.85 | 2.32 | 42 MB |
| overlay2 today, local registry | torchserve 0.12.0-cpu | 19.92 | 7.20 | 27.21 | 702 MB |
| overlay2 today, Docker Hub | torchserve 0.12.0-cpu | 16.79 | 7.34 | 24.13 | 702 MB |
| lazy, S3 | torchserve 0.12.0-cpu | 0.65 | 12.54 | 13.28 | 134 MB |

A warm second torch run took 2.7 s. The cold torch run waits on 116 frame
fetches made one after another (p50 42 ms, p90 197 ms, 9.7 s in all): the
import touches small files spread over the layer. What did not help:
reading 3 frames ahead in a file, fetching a large file's frames on its
first read (both 12.5 s, more bytes), and filling the 1 GB layers whole
(16.8 s, 644 MB: the uncompressed frames saturate the 125 MB/s gp3 disk).

### Review fixes, CI and the local host VM

- Mounts a snapshot uses stay mounted at exit; an unmount is one
  non-blocking syscall, its server stopped in the background
  (`TestStopLeavesLayersInUseMounted`). Shared fetches run under the
  snapshotter's life, so a cancelled fill fails no read
  (`TestCancelledFillsFinishSharedFetches`); evictions delete a frame's file
  under a per-frame lock only if it was not stored again
  (`TestEvictionKeepsAFrameStoredAgain`). Indexes are read with
  `imagefs.FetchIndex`, digests checked with `imagefs.Digest.Check`; the
  tests run as root.
- Start commands in the agent harness carry the grants of images converted
  by `imagefs.Convert` into the job's Garage: `TestAgentPullsAMissingImage`
  (grants then lazy pull, no layer blob stored) and
  `TestAStartWithoutGrantsFails`. The Go workflow's `host-runtime` job
  installs the snapshotter on the runner first.
- `deploy/local/host-vm.sh`: a Lima VM from the AL2023 KVM image runs the
  node recipe, joins the local server through the install script, and ran a
  deployed function from the converted managed image through the
  snapshotter (3 lazy layers, only the config fetched by containerd). The
  VM runs kernel 6.1, the KVM image's.

## Intentional differences

- Lazy layers live beside containerd's overlay snapshotter in one service;
  every snapshot that is not a lazy layer is a plain overlay snapshot, so
  Docker builds and platform images work unchanged.
- A layer reused by chain ID (the same diff_ids under another reference)
  is read lazily too, and needs a grant like any other.
- Grants are memory only: after a snapshotter restart reads fail with EIO
  until the agent grants again.
- The cache keeps uncompressed frames and starts empty at each start.

## Gaps and unverified boundaries

- Platform images (the mount image, the BuildKit builder) still unpack
  plainly, so one can be served by a tenant's lazy layer of the same chain
  ID and stop reading when that grant expires. The proposed fix, mirroring
  them through publish, cannot convert the builder: a mirror build needs the
  builder. Open for a decision.
- Docker's writable-layer size limits are not enforced on the lazycloud
  driver (the agent warns at start); overlay2 enforced them with xfs quotas.
- The node image is not rebaked; its settings were applied by script.
- ECR as the registry, Docker 29.9 and GPU images are untried.
- Torch cold start stays round-trip bound; a startup prefetch list per
  image or the peer cache is the next lever.
