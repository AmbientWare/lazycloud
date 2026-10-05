# Spike: Docker, a remote snapshotter and gVisor

## Scope

Answer the four questions in plan.md with evidence, before any product code.
Scratch code and configuration only; nothing here merges. Owns nothing in
the tree except this file.

## Plan

1. **Local machine.** Skip to step 2 if starting a second dockerd needs a password you do not have. Docker 29.6 with containerd 2.2 runs here. Do not change
   the user's Docker or containerd configuration. Run a separate dockerd and
   containerd pair with their own root, state and socket directories (for
   example under `/tmp/lazy-spike`), started and stopped by you, with the
   containerd image store enabled and the stargz snapshotter registered as a
   proxy plugin. Install pinned releases of stargz-snapshotter and gVisor
   into the scratch directory, checksums verified.
   - Convert a public Python image to eStargz and push it to a scratch
     registry container you start.
   - `docker pull` and `docker run` it through the scratch daemon with runc,
     then with runsc. Record whether layer blobs were downloaded (registry
     logs, containerd content store), whether the container reads its files,
     and its start time.
   - Restart the snapshotter's caller side (stop and start your stand-in for
     the agent, not the snapshotter) while a container reads files, and
     record what the container sees.
2. **EC2 host from the node image.** One on-demand or Spot m7i.large in
   `us-east-2`, `AWS_PROFILE=default`, tagged `lazycloud:task=perf-lazy-spike`,
   launched from the CPU node image the fleet uses (see
   `deploy/helm/lazycloud/environments/prod.yaml` for the image IDs), with no
   agent user data. Repeat step 1's runs there with the host's own Docker
   and runsc, configured the way a node image would carry it.
   - Cold start of the converted Python image on that fresh host against a
     plain `docker pull` of the original, three runs each.
   - The same for a large image (a public PyTorch CPU image).
   - Terminate the instance and anything it created before reporting.
3. Write down the exact daemon and containerd settings that worked, the
   snapshotter's labels and handler behavior Docker relied on, and anything
   gVisor needed (mount options, gofer settings, FUSE device access).

## Evidence to record

For each question: yes or no, the command output that shows it, and the
settings. The cold start numbers as a table. What did not work and why.

## Progress

Done on 2026-10-05, on EC2 only. The answers: 1 yes, 2 yes, 3 yes, 4 measured.
Go for wave 2, with the Docker version and restart constraints below.

- Local step skipped. There is no passwordless sudo here, so no second
  dockerd.
- EC2 host: one on-demand m7i.large in us-east-2a from the fleet CPU image
  ami-0e3303f49502924b3 (lazycloud-node-cpu-d0afedf4e47c63ba), 40 GiB gp3 at
  the default 125 MB/s, instance profile lazycloud-prod-fleet-node (SSM only),
  no agent user data. The default VPC in us-east-2 has no internet route, so
  the first instance never reached SSM and was terminated. The second ran in
  a scratch VPC the spike created and deleted.
- The node image carries Docker 25.0.16 (Amazon Linux package) on the overlay2
  graphdriver, containerd 2.2.7, runc 1.3.6, runsc release-20260928.0, kernel
  6.18.51, and /dev/fuse. It is not Docker 29.
- Installed on the instance only: stargz-snapshotter v0.18.2 (sha256
  515a3c3a..., matches the release's .sha256sum) and the Docker static
  releases 29.8.2 and 29.9.0-rc.1. download.docker.com publishes no checksums
  for those, so each was pinned to the sha256 of an independent download
  (995d1ef2..., 408e3702...). Docker 29 ran against the image's containerd
  2.2.7.
- Images, linux/amd64: python:3.12-slim (index sha256:02108f5d..., 4 layers,
  46 MB compressed) and pytorch/torchserve:0.12.0-cpu (index
  sha256:50e18949..., 9 layers, 702 MB compressed, torch 2.4.0+cpu). The
  originals went byte for byte into a scratch registry:3 on the instance
  (tag suffix `-org`). `ctr-remote i convert --oci --estargz` produced the
  `-esgz` tags in 14 s and 166 s. The registry stored blobs on a tmpfs and
  served over loopback.

## Evidence

### 1. Docker skips layer downloads with a remote snapshotter: yes, up to Docker 29.8

Docker 29.8.2 with the containerd image store and `storage-driver: stargz`
pulled the eStargz images without downloading layer blobs. The registry sent
only the footers and TOCs, and no layer blob reached containerd's content
store:

```
docker-29.8.2 python:3.12-slim-esgz       pull=0.48s registry->host 0.4MB of 49.9MB layers; layers in content store 0/4
docker-29.8.2 torchserve:0.12.0-cpu-esgz  pull=0.83s registry->host 2.8MB of 727.7MB layers; layers in content store 0/9
docker-29.8.2 torchserve:0.12.0-cpu-org   pull=20.1s registry sent 702MB;                 layers in content store 9/9
docker-29.9.0-rc.1 torchserve:0.12.0-cpu-esgz pull=6.18s registry sent 732MB;              layers in content store 9/9
docker-25 torchserve:0.12.0-cpu-esgz      pull=0.73s registry->host 2.8MB of 727.7MB layers; layers in content store 0/9
```

Docker 29.9.0-rc.1 breaks this. Its pull sets
`containerd.WithUnpackFetchAllContent()` in `daemon/containerd/image_pull.go`
(moby#49784: keep blobs so an image can be saved or pushed). The unpacker then
downloads every layer even when the snapshotter reports the snapshot as
existing. It is in 29.9.0-rc.1 and on moby master, and not in any 29.8 or
earlier tag. Lazy reads still work under 29.9, but the pull costs a full
download again. One workaround works on 29.9: pull through containerd's API
into Docker's namespace, then let Docker run it.

```
dockerd 29.9.0-rc.1
rpull into namespace moby: .89s, registry sent 2MB      (ctr-remote -n moby image rpull --snapshotter stargz)
docker run ok 2.4.0+cpu
run: 7.23s, registry sent 100MB
```

The node image's own Docker 25.0.16 also pulls lazily once its daemon.json
enables the containerd image store and the stargz driver, with the same cold
starts (table below). So lazy pulls need the containerd image store and the
snapshotter as storage driver, on Docker 25.0 to 29.8. On 29.9 and later
they also need the agent to pull through containerd itself. The node image
does not have to move to Docker 29 for this.

Labels Docker relied on, from the containerd client vendored in dockerd
(v2.3.6 in 29.8.2, v2.4.1 in 29.9.0-rc.1, `core/unpack/unpacker.go`): every
layer Prepare carries `containerd.io/snapshot.ref` (the chain ID),
`containerd.io/snapshot/diff-id` (the layer's uncompressed digest), the
parent chain ID, and the descriptor annotations under `containerd.io/snapshot/`.
Docker adds those annotations through `snapshotters.AppendInfoHandlerWrapper`:
`cri.image-ref`, `cri.layer-digest`, `cri.manifest-digest` and
`cri.image-layers`. When Prepare returns AlreadyExists and Stat finds the
chain ID, the unpacker skips that layer's fetch and apply. The diff-id label
is what our index is keyed by, so our snapshotter needs nothing
stargz-specific from Docker.

### 2. gVisor runs containers on FUSE lower layers: yes

Docker 29.8.2 ran the eStargz images under runsc with the node image's
runtime entry (`--host-uds=all`) and default runsc flags (`overlay2
root:self`, `directfs true`). gVisor needed no mount options, gofer
settings or FUSE device. The FUSE mounts live on the host. The runsc shim
mounts the host overlay, and the gofer serves it into the sandbox.

```
overlay /var/lib/docker/rootfs/stargz/<id> overlay rw
   upperdir=/var/lib/containerd-stargz-grpc/snapshotter/snapshots/447/fs
   lower .../snapshots/446/fs: xfs        (the -init layer)
   lower .../snapshots/413/fs: fuseblk    (eStargz layers, 4 of them)
```

The file tree matches the plain image exactly. A list of 4920 files and
symlinks under usr, etc, bin, sbin, lib, lib64, var and opt, with mode, owner,
size and sha256 of each, was identical between eStargz under runsc and the
original image under runc. Only /etc/hostname and /etc/hosts differ, and
Docker writes those per container. Inside the sandbox, a write to /etc landed
in the upper layer, and deleting /usr/local/bin/pip from a lower layer hid it
(whiteout). `import torch` ran on the lazily mounted torch image. Inside
gVisor the root shows as `none on / type overlay`, gVisor's own overlay, so
writes go to its self-backed file store and not the host upperdir.

### 3. A FUSE mount held by a separate service survives a restart of its callers: yes

A runsc container read every file of the torch install, 17484 files, one
every 20 ms, from a cold cache. The script then restarted things around it:

```
restart agent stand-in (a unit following docker logs)  container running, err=0
restart containerd                                      container running, err=0
restart dockerd (live-restore)                          container running, ok=500 err=0
restart containerd-stargz-grpc, FUSE manager on         container running, ok=1000 err=0 afterwards
restart containerd-stargz-grpc, FUSE manager off        893 failed reads: "Transport endpoint is not connected"
```

Restarting the process that serves FUSE breaks every running container, even
though stargz mounts fresh copies right away. The container's overlay still
points at the dead mounts. stargz avoids this with its FUSE manager, a
detached process that holds /dev/fuse, so the gRPC side can restart. Under
systemd that only works with `KillMode=process`. The default control-group
kill takes the manager down with the unit. Our snapshotter must hold its FUSE
servers in a process that agent updates never restart.

A restarted stargz also re-resolves every remote snapshot from the registry
before it serves. With the registry unreachable it refused to start until
`allow_invalid_mounts_on_restart = true`. Our snapshotter should restore from
its local index cache and not need the store at startup.

### 4. Cold start on a fresh host: torch 25.2 s to 8.4 s, python 2.8 s to 2.5 s

Median of three runs. Each run removes the image, wipes the snapshotter
caches, drops the page cache, then times `docker pull` and one
`docker run --runtime runsc`. The python command imports json, sqlite3,
asyncio and ssl. The torch command imports torch and sums a tensor.

| Host setup | Image | Pull | Run | Total |
| --- | --- | --- | --- | --- |
| Docker 25.0.16 overlay2 (node image today) | python:3.12-slim | 1.41 | 1.37 | 2.79 |
| Docker 29.8.2 containerd store, overlayfs | python:3.12-slim | 1.45 | 1.51 | 2.96 |
| Docker 29.8.2 stargz, eStargz | python:3.12-slim | 0.49 | 2.03 | 2.51 |
| Docker 25.0.16 stargz, eStargz (1 run) | python:3.12-slim | 0.40 | 2.11 | 2.51 |
| Docker 25.0.16 overlay2 (node image today) | torchserve 0.12.0-cpu | 18.20 | 6.89 | 25.15 |
| Docker 29.8.2 containerd store, overlayfs | torchserve 0.12.0-cpu | 38.01 | 15.10 | 53.12 |
| Docker 29.8.2 stargz, eStargz | torchserve 0.12.0-cpu | 0.81 | 7.64 | 8.45 |
| Docker 25.0.16 stargz, eStargz (1 run) | torchserve 0.12.0-cpu | 0.72 | 8.25 | 8.97 |
| Docker 29.8.2 stargz, plain image (1 run) | torchserve 0.12.0-cpu | 35.10 | 16.28 | 51.37 |

Bytes read on demand, with background fetch off: the python cold run fetched
11 MB of 50 MB and the torch cold run 105 MB of 728 MB. A warm second run took
0.8 s for python and 2.5 s for torch, so lazy reads add about 1.2 s to the
python start and 5 s to torch. Those costs come from round trips more than
bandwidth, and a prefetch list of the files an image reads at start would
attack them. stargz's default background fetch then pulled whole layers
behind the container. It did not change the cold start.

Three readings of the table:

- The containerd image store without a lazy snapshotter is twice as slow as
  today's overlay2 for torch. It writes the compressed blobs and the extracted
  tree to a 125 MB/s gp3 root volume, and the writeback slows the first run
  too. Switching the store without lazy layers would be a regression.
- A plain image on the stargz driver is just as slow. Every image needs a
  lazy index before a host runs it, which matches the hard cut in the plan.
- These numbers are an upper bound on the pull side only. The registry was a
  tmpfs over loopback, so plain pulls paid no network time. From ECR or S3
  the plain pull gets slower and the lazy gain grows.

### Settings that worked

`/etc/docker/daemon.json`, the node image's file plus three keys:

```json
{
  "runtimes": {"runsc": {"path": "/usr/local/lib/gvisor/20260928/runsc", "runtimeArgs": ["--host-uds=all"]}},
  "cgroup-parent": "lazycloud-workloads.slice",
  "features": {"containerd-snapshotter": true},
  "storage-driver": "stargz",
  "live-restore": true
}
```

Appended to `/etc/containerd/config.toml` (version 3), whose
`[proxy_plugins]` table already lists soci from the Amazon Linux package. The
exports were not tested separately.

```toml
  [proxy_plugins.stargz]
    type = "snapshot"
    address = "/run/containerd-stargz-grpc/containerd-stargz-grpc.sock"
    [proxy_plugins.stargz.exports]
      root = "/var/lib/containerd-stargz-grpc/"
      enable_remote_snapshot_annotations = "true"
```

`/etc/containerd-stargz-grpc/config.toml`:

```toml
allow_invalid_mounts_on_restart = true
[[resolver.host."127.0.0.1:5000".mirrors]]
host = "127.0.0.1:5000"
insecure = true
[fuse_manager]
enable = true
address = "/run/containerd-stargz-grpc/fuse-manager.sock"
path = "/usr/local/bin/stargz-fuse-manager"
```

The snapshotter ran as its own unit (`Type=notify`, `Before=containerd.service`,
`Restart=always`, `KillMode=process`). containerd restarts after the proxy
plugin is added, and Docker restarts after the storage driver changes. Docker
29 ran from a drop-in that replaces `ExecStart` with the static dockerd and
keeps `--containerd=/run/containerd/containerd.sock`.

### What did not work

- The us-east-2 default VPC has no route to the internet, so SSM never
  registered there.
- Docker 29.9.0-rc.1 pulls every layer blob, as described in question 1.
- Restarting the FUSE server without the FUSE manager broke running
  containers.
- stargz refused to start while the registry was down until
  `allow_invalid_mounts_on_restart` was set.
- `ctr push` of a multi-platform index fails on registry:3 when only one
  platform is local. The spike copied the amd64 manifest and blobs with a
  small script instead.
- Content in another containerd namespace is shared on pull
  (`content_sharing_policy = "shared"`), so the first Docker 29 baselines
  skipped downloads. The conversion namespace was deleted and those runs
  repeated; the table has only the clean runs.

## Gaps and unverified boundaries

- No ECR or S3 in the path. The registry was a local tmpfs, so network time
  and per-request latency to a real store are not measured. The acceptance
  packet measures them.
- One m7i.large in one zone. GPU images, other instance types and many
  containers starting at once were not tried.
- Docker 25 with stargz got one run per image. Docker 29.9 behaviour is from
  an rc, and the final 29.9 release may differ.
- gVisor checks covered file contents, metadata, writes and whiteouts.
  Hard links, xattrs, device nodes and mmap-heavy loads beyond `import torch`
  were not checked separately.
- The restart test used stargz's FUSE manager, not our snapshotter. Restarting
  the FUSE manager itself, and holding mounts across a host reboot, were not
  tested; the stargz docs say both drop the mounts.
- Cleanup: both instances (i-04dffcfdcec45987b, i-02addbb104bb86882) are
  terminated and their volumes deleted with them. Deleted: security groups
  sg-0700aa9eb06779721 and sg-0e7f5e9a5e9ec0c4f, route table association
  rtbassoc-0e03a49f4c59c0449, route table rtb-0c059dc7d3ab1958f, internet
  gateway igw-0310d881d9eebf511, subnet subnet-032dbb6c1cddbdb2a, VPC
  vpc-0a9670091c5cca7f9. No ECR repository or key pair was created.
