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

Done on 2026-10-05 on `perf-lazy-publish`. `LayerReadURLs` landed first in
89fc2ecd.

- Migration 0003: `image_layers` (one pair per blob and trust scope, keyed
  by uuid so concurrent uploads never share an object), `image_reference_layers`
  (a converted reference's layers in order), `image_layer_uploads` (pairs that
  may exist without a row: offered uploads with their multipart upload, lost
  races, retired pairs), `image_reference_uses`, `managed_images`, and
  `image_builds.mirror` and `failure_transient`. It reads and writes no table
  it creates and rewrites no existing row.
- Objects are `layers/<id>/index` and `layers/<id>/data` in the platform
  bucket (`internal/storage/layers.go`). Data goes up as a multipart upload in
  64 MiB parts; every part URL and the index URL is signed for its exact
  Content-Length, so a host stores no more than it reported, and a layer's
  reported sizes are bounded by its blob's size.
- Publish rule: `CompleteBuild` reads the pushed manifest and config from the
  registry itself and publishes only when every blob has a usable pair. The
  host is told which layers to convert, reports their sizes
  (`converted_layers`, field 90), gets the signed URLs, uploads, and reports
  the part ETags (`uploaded_layers`, field 91). The server completes the
  upload, checks the index against the config's diff_id and the data size,
  and records the pair. Repeated reports get the same uploads. A finished
  build aborts its container's open uploads; the sweep deletes their objects
  once their URLs lapse and aborts stale uploads.
- Trust: pairs are matched by compressed blob digest, which the registry
  vouches for. A scoped build's pairs (a forced rebuild, a connected
  workspace's build, a mirror of a workspace's own image) serve only that
  workspace. Only public and platform-global references mirror on platform
  hosts into shared pairs; a workspace's own references mirror where its
  builds run, into its own repository. Planner demand follows placement.
- A reference is published only while it has layer rows. Every definition
  builds, base-only ones and filesystem images too. A definition whose
  reference lost its layer rows converts again by mirroring that reference,
  never by running its steps again.
- Mirror builds (no steps) convert what has no layer rows: the managed Python
  image (`ManagedPull`), a pinned reference at start (`ConvertedPull`), and a
  stored reference at deploy (`Deployable`). The first request starts one,
  others join it read-only and get `*images.BuildWaitError` (also
  `ErrNotReady`); the session wakes on that build's id. A failure of the
  image (its content, its deadline, every attempt lost) is
  `*images.ConversionError` for a minute; a transient one (a lost first
  attempt, an unreachable store, a host error) builds again on the next
  request.
- Sweep (`SweepLayers`, every 10 minutes): live references are those pinned
  by releases that can still start (active releases of live workloads and
  apps, previews, releases with containers or tasks under way), the managed
  images, and references published or started within 24 hours. Pairs unused
  for 24 hours retire, unconverting every reference that used them, and
  their objects go; older use rows are purged.
- Agent: converts the named layers (4 at once) to temp files, reports sizes,
  uploads parts then index, reports ETags. Registry reads and PUTs retry with
  backoff; only content failures fail the image.

- `Image.from_id` of an image not ready calls `prepareImage` (a new API
  operation) and follows the build it returns, with the same progress as a
  definition build; a conversion that cannot run is the build failure.

## Evidence

Owner tests, `go test -race`, real PostgreSQL, Garage and a registry:

- storage: `TestLayerDataUploadsInSignedParts` (3 parts of a 128 MiB+ object
  read back whole; a longer part and index answer 403; abort; empty data).
- images: `TestLayerReadURLs`, `TestImagePublishesOnceEveryLayerIsConverted`,
  `TestConcurrentConversionsOfOneLayerEndWithOneRecord`,
  `TestCustomerHostLayersServeOnlyTheirWorkspace`,
  `TestLayerUploadsAreBoundedAndAbortedWithTheirBuild`,
  `TestSweepRetiresOnlyPairsNoLiveImageUses` (live set, starts, no image
  history, whole-reference retire), `TestManagedImageIsBuiltOnceAndPublishedConverted`,
  `TestDeployConvertsAStoredReferenceWithoutLayers`,
  `TestTransientMirrorFailuresBuildAgain`, `TestMirrorBuildsRunOnPlatformHosts`,
  `TestAWorkspacesOwnReferenceConvertsOnItsOwnHosts`,
  `TestASweptImageReconvertsByMirroringItsReference`,
  `TestAMirrorThatExhaustsItsAttemptsFailsTheImage`,
  `TestFilesystemImagesPublishConverted`, `TestPinnedReferencesConvertOnceOrFailTyped`.
- hostsession: `TestStartWaitsForTheManagedImageBuild` (waiting syncs write
  nothing), `TestBuildWaitsWakeOnlyForTheirBuilds`.
- agent: `TestAgentConvertsTheLayersTheServerNames` (real BuildKit build,
  multipart upload, every first PUT answered 503 and retried),
  `TestHostErrorsAreNotLayerContent`.
- compute: `TestMirrorBuildDemandIsThePlatforms`.

Local stack (own compose project, ports 26xxx/28xxx, runc), python:3.12-slim
base, conversion time from the build logs:

| Build | Layers converted | Conversion |
| --- | --- | --- |
| `pip install six` | 5 of 5 (base 4 + 1), 48 MB stored | 696 to 757 ms |
| `pip install idna`, same base | 1 of 5 | 102 to 112 ms |
| no steps (`Image()` and no-image function, 3.12) | 0 of 4 | 0 |
| no-image function, 3.11 | 3 of 4 (debian base shared) | 371 ms |
| release whose pinned reference lost its rows | 0, mirror build 0.8 s | call 1.1 s |

Garage held every pair at the recorded sizes. Calls on converted images took
0.5 s. The multipart run first failed on a layer with an empty data object
(Garage refuses an empty part); empty data is now stored at once with no
parts.

## Intentional differences

- Images without layer rows are not converted in place: they read as
  unpublished, a deploy or start converts the stored or pinned reference
  through a mirror build and waits for it.
- A definition that only names its base, and a filesystem image a host
  pushed, are mirror builds; hosts never pull workload images from other
  registries.
- `LayerReadURLs` takes the pinned reference, not the image id: a workspace
  rebuild gives an image a new reference while older releases still run the
  old one.

## Gaps and unverified boundaries

- A converted layer waits on the host's disk until all named layers are
  converted and uploaded: disk use is the build's converted size.
- A start waits at most execution's 10 minute start timeout for a mirror.
- S3 and ECR are unverified here; the acceptance packet runs them. The
  acceptance harness seeds a published managed image instead of building one.
