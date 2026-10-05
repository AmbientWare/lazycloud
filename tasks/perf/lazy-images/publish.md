# Publish

## Scope

Every image a build produces is converted before images publishes it. Owns:

- `migrations/0003_image_layers.sql`: converted layers by `diff_id` (index and
  data keys, sizes, entry and frame counts) and the layers of each image in
  order. The migration reads and writes no table it creates.
- `internal/images`: the publish rule (an image publishes only when every
  layer is converted), presigned upload URLs for missing layers, the live
  references, and the sweep of layer pairs no live image references.
- The build path in the agent (`internal/agent/build.go` and the code it
  calls): after the push, convert each layer the server asks for with
  `imagefs.Convert`, stream `data` and `index` to the presigned URLs, report.
- `contracts/host/v1/host.proto`: the build request and report fields,
  numbers 90-99 in each message it extends. Regenerate bindings.

Stay off the snapshotter, the agent's image pull, `internal/imagefs` (use its
contract; propose changes) and compute.

## Plan

- The server decides which layers to convert: the build reports the pushed
  image's layers, the server answers with upload URLs only for layers not
  yet converted, and records a layer once its upload is reported. Two builds
  converting the same base layer at once end with one record; a lost report
  is retried without converting twice.
- Images built before this ships are not converted in place; a rebuild
  converts them (hard cut). Say in the report how a deployed app's next
  start behaves.
- The sweep reads live image references, never history, and deletes a pair
  only after no live image has referenced it for a grace period long enough
  for a build in progress.

## Evidence to record

- Owner tests against the real test Postgres for the publish rule, the
  concurrent conversion of one base layer, and the sweep.
- A real build on a local stack that publishes a converted image with its
  pairs in Garage; a second image on the same base converts only its new
  layers.

## Progress

## Intentional differences

## Gaps and unverified boundaries
