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

## Intentional differences

## Gaps and unverified boundaries
