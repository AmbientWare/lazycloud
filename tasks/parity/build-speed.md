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

## Gaps and unverified boundaries
