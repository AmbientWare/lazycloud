# Build speed

## Scope

The torch build took 69.7 s against Beam's 22.2 s. Excluding the package
install (base-python's), the measured parts were BuildKit exporting layers
(24.1 s), pushing (4.8 s), its cache export (12 s on another build) and our
conversion (9.3 s; 20.8 s on a 368 MB layer, about 20 MB/s, while
conversion alone runs about 230 MB/s locally). Owns the agent's build runner
(`internal/agent/build.go` and what it runs BuildKit with) and the layer
conversion and upload (`internal/agent/convert.go`, `imagefs.UploadPair`).

## Plan

- Upload multipart parts in parallel (bounded) and overlap conversion with
  upload, so a layer's time is about its conversion time.
- Start converting as soon as the image push completes; BuildKit's cache
  export runs alongside instead of before.
- Measure BuildKit's layer export and push with zstd compression against
  gzip; take whichever is faster end to end, conversion included.
- Keep every bound and retry the reviews established.

## Evidence to record

- Before and after on the same torch image on a local stack and on EC2:
  export, push, cache export, conversion, total.

## Progress

2026-10-06, PR #505:

- `imagefs.UploadPair` PUTs up to `UploadParts` (4) data parts at once;
  the index still goes last, a failed part ends the others. Tests:
  `TestUploadPairPutsPartsInParallelAndTheIndexLast`,
  `TestUploadPairStopsAtARefusedPart`.
- `layerPublish` runs each conversion and upload in its own goroutine
  (at most 4 of each) and reports each layer's sizes and ETags as they
  finish, so layers upload while others convert. `maxPublishRounds` now
  bounds the times one layer is acted on. The data file is no longer
  synced. Tests: `TestLayerPublishUploadsALayerWhileAnotherConverts`,
  `TestLayerPublishBoundsTheRoundsOfALayer`,
  `TestAgentConvertsTheLayersTheServerNames`.
- The builder runs two solves: the first pushes the image and writes its
  metadata, the agent starts publishing on the line that follows, and a
  second solve (all steps cached) exports the cache meanwhile. A cache
  export failure after the push no longer fails the build. Test:
  `TestAgentBuildsPushesAndPullsAnImageByDigest` (reported while the
  builder runs, cache exported after).
- Layers and cache are zstd.

Torch CPU on the uv 3.12 base, base layers counted as converted, seconds
from the start of "exporting to image" to the last layer stored:

| | export | push | cache export | conversion (torch layer) | total |
| --- | --- | --- | --- | --- | --- |
| EC2 m7i.large, before (gzip) | 29.9 / 30.0 | 7.2 / 4.8 | 7.2 / 7.6, beside push | 8.1 / 8.2, then upload 1.9 | 47.3 / 47.8 |
| EC2, after, gzip | 30.0 | 3.8 | beside conversion | 9.1, then upload 0.7 | 43.4 |
| EC2, after (zstd) | 13.2 / 13.2 | 3.4 / 3.3 | beside conversion | 7.5 / 7.5, then upload 0.7 | 24.8 / 24.6 |
| local, before (gzip) | 15.8 | 1.4 | 2.5, beside push | 5.9 | 25.3 |
| local, after (zstd) | 3.2 | 0.5 | 3.3, beside conversion | 4.1, then upload 0.2 | 8.2 |

EC2 builds ran through the real agent with the host's snapshotter,
multipart uploads signed on a scratch S3 bucket and a registry on the
host; whole builds took 72.2 / 70.9 s before and 47.6 / 47.5 s after.
Local numbers combine the builder run as the agent runs it with the
publish loop against a test Garage, since this machine has no
snapshotter.

## Gaps and unverified boundaries

- The registry was a local one on each host, not ECR; ECR push and cache
  export times are unmeasured.
- Conversion of the torch layer (854 MB of tar at about 114 MB/s on two
  vCPUs) is now the largest cost after the push; `imagefs.Convert`
  compresses frames on one core. It is outside this packet.
- zstd export on two vCPUs still takes 13 s for that layer.
