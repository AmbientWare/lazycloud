# Snapshotter

## Scope

The host service that gives Docker lazily filled layers. Owns:

- a `cmd/agent` subcommand that runs the service, and its systemd unit,
  separate from the agent's so agent restarts and updates leave it running;
- new package `internal/imagefs/snapshotter` (or the layout the format packet
  settles): the containerd snapshotter API, one FUSE filesystem per mounted
  layer, the local chunk cache with a disk bound and LRU eviction that never
  evicts chunks a running container holds open;
- `deploy/ami/node-setup.sh` and `deploy/local/host-setup.sh`: the Docker and
  containerd settings the spike proved.

Stay off images, the build path, compute and the host protocol.

## Plan

Refined from the spike's report before work starts. It covers mount
lifetimes, cleanup of mounts whose container is gone, the cache bound, and
what a read sees when a chunk cannot be fetched (an I/O error to the
container, logged and counted, never a silent empty file).

## Evidence to record

- A container on a local stack runs from an indexed image with no layer blob
  downloaded, under runc and under runsc.
- The service keeps serving a running container's reads across an agent
  restart.
- The cache stays under its bound while two large images alternate.

## Progress

## Intentional differences

## Gaps and unverified boundaries
