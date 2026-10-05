# Grants

## Scope

A host reads only the layers of images it was told to run. Owns:

- the server side: with each start command, presigned GET URLs for the
  `index` and `data` of every layer of the container's image, and fresh URLs
  before they expire for as long as a container on the host uses the layer
  (the way `StorageGrant` refreshes);
- `contracts/host/v1/host.proto` fields 100-109 for them;
- the agent side: hand the URLs to the snapshotter over the local interface,
  and refresh them. Propose that interface in your first commit (a small
  local gRPC or HTTP API on the snapshotter's socket); the integrator settles
  it with the snapshotter packet.

Stay off the snapshotter's internals, the build path and `internal/imagefs`.

## Plan

- Presigning is local computation; no store call per start. Choose an expiry
  and refresh margin and say why.
- The same path serves platform, connected-account and self-hosted hosts.

## Evidence to record

- A host sent one workspace's image cannot read another layer with what it
  was sent: a URL for one object does not open another, and an expired URL
  stops working.
- A long-running container keeps reading past the first expiry.
- Owner tests for which layers a start command carries, against the real
  test Postgres.

## Progress

## Intentional differences

## Gaps and unverified boundaries
