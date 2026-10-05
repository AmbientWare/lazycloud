# Publish

## Scope

Every image a build produces is indexed before images publishes it. Owns:

- `migrations/0003_image_layers.sql`: which layers (by `diff_id`) are indexed,
  their index key, size and chunk count, and which image uses which layer.
  The migration reads and writes no table it creates.
- `internal/images`: the publish rule (an image publishes only when every
  layer is indexed), queries for indexed layers, and the live references the
  chunk sweep reads.
- `internal/agent/build.go` and the build path: after the push, index each
  layer the store does not already have and report it.
- `contracts/host/v1/host.proto`: the build report fields, numbers 90-99 in
  each message it extends. Regenerate bindings.

Stay off the snapshotter, `internal/imagefs` internals (use its API; propose
changes) and compute.

## Plan

Refined from the spike's report and the format packet's API before work
starts. It covers retry and idempotency (a build retried after a lost report
indexes nothing twice), layers shared across images and workspaces, and the
sweep of chunks and indexes no live image references.

## Evidence to record

- Owner tests against the real test Postgres for the publish rule and the
  live-reference query.
- A real build on a local stack that publishes an indexed image, with
  Garage holding its chunks; a second image on the same base indexes only
  its new layers.

## Progress

## Intentional differences

## Gaps and unverified boundaries
