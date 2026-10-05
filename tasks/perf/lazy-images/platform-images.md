# Platform images

## Scope

The images the agent itself runs (the BuildKit builder, the volume mount
image, the pod network holder and any other image the agent pulls on its own)
come through the snapshotter with their own grants, like every workload
image. Today they pull plainly, and containerd reuses a snapshot by chain ID,
so a platform image can end up reading through a tenant's lazy layer and its
grant (snapshotter review, finding 2).

Owns:

- `internal/images`: server-side conversion of platform-global references
  without a build container: copy the image into the platform registry,
  convert each layer with `imagefs.Convert`, record it as a platform-global
  image. Deduped (one conversion per reference across server replicas, by an
  advisory lock or the existing build dedupe), retried on transient errors.
- `contracts/host/v1/host.proto` fields 110-119: the agent names its platform
  image references at session open.
- `internal/hostsession`: convert them on demand and send their grants with
  the session and its refreshes, through the grants path.
- the agent: pull them through `ensureLazy`; delete the plain `docker pull`
  path in `internal/agent/source.go`.

Stay off the snapshotter's internals, the build path and the format.

## Evidence to record

- Owner tests against real Postgres for dedupe and failure.
- An agent harness test: a platform image and a tenant image sharing a base
  layer each read through their own grant; expiring the tenant's grant leaves
  the platform image readable.
- A local VM run (`deploy/local/host-vm.sh`) where a build and a volume mount
  run with no plain pull.

## Progress

## Intentional differences

## Gaps and unverified boundaries
