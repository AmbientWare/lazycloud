# Prefetch

## Scope

A cold start of a large image fetches the frames its container reads at
startup in parallel instead of one after another. The snapshotter packet
measured torch from S3 at 13.3 s, of which the run was 12.5 s: 116 frame
fetches in sequence, p50 42 ms, p90 197 ms. Reading ahead within a file and
fetching whole 1 GB layers did not help.

Owns the snapshotter's read trace and prefetch, and wherever the trace is
stored. Stay off the format's encoding unless the report proposes a change.

## Plan

- The first containers started from an image record which frames they read
  before they report ready, in order, per layer; the server keeps one trace
  per image reference, the way beta9 records CLIP read events.
- Later cold starts of that reference fetch the traced frames in parallel
  (bounded concurrency, highest first) as soon as the layers mount, while the
  container's own reads take priority.
- A trace is small (frame numbers per layer), bounded in size, and replaced
  when it ages; an image with no trace starts as today.

## Evidence to record

- Torch and python cold starts from S3 on a fresh EC2 host, with and without
  a trace, three runs each, next to the snapshotter packet's table.
- Bytes fetched with a trace against without: prefetch must not read more
  than the startup needs.

## Progress

Done on 2026-10-05 on `perf-lazy-prefetch`, draft PR #494 into `perf-plan`.

- Snapshotter (`internal/imagefs/snapshotter/trace.go`): `LayerSources`
  gains `StartTrace`, `EndTrace`, `Prefetch` and `StopPrefetch`. A trace records each frame
  of its layers the first time a FUSE read reaches it, in order, by layer
  position. A FUSE read cannot be tied to a container, so a trace is
  complete only if none of its layers was mounted when it started and no
  Grant, Prefetch or StartTrace under another name (or a nameless refresh)
  named one of them before it ended. At most 64 traces of 4096 frames each;
  one nobody ends expires after 15 minutes, dropped on the next read, start
  or end. A prefetch, named by its container, fetches its frames in order as
  each layer mounts, through the same shared fetches as reads, skipping
  cached frames and layers gone since; it stops on `StopPrefetch` or when a
  layer has not mounted 30 s after it began. It takes the background slots
  fills use, half of `--fetches`, so a container's reads always find a
  fetch slot. At most 16 prefetches run, each for at most 5 minutes and a
  quarter of the cache. Client:
  `layersource.Client.{Prefetch,StopPrefetch,StartTrace,EndTrace}`.
- Host protocol: `StartContainer.prefetch = 120` (`ImageTrace`),
  `StartContainer.record_trace = 121`, `HostMessage.startup_trace = 120`
  (`StartupTrace`).
- Agent: before the pull, a start hands its prefetch to the snapshotter and
  starts a trace when asked; when the container reports ready, the agent
  ends the trace and reports it if complete and no other start on the host
  used one of its layers meanwhile; a start sharing a layer with another
  records no trace. An exit or failed start stops its prefetch and ends its
  trace unreported. Both are best effort: a refusal is logged, the start
  goes on.
- Server: migration 0005 `image_traces`, one trace per workspace and
  reference, its use row's child (`image_reference_uses`), so the layer
  sweep's purge drops it. Starts carry the trace and ask for a new one when
  there is none or it is a day old; one query per reference per sync. A
  reported trace is stored under the container's own workspace and image
  (`execution.ContainerImage`, live containers on the reporting host only),
  checked against the reference's layer frame counts.

## Evidence

- Owner tests: `TestTracesRecordFirstReadsInOrder`, `TestTracesAreBounded`,
  `TestPrefetchFetchesTracedFramesOnceMounted`,
  `TestPrefetchesLeaveSlotsForReads`, `TestSharedLayersLeaveNoCompleteTrace`,
  `TestFailedStartsNeverExhaustPrefetches` (frame cache, Garage),
  `TestTracedFramesPrefetchThroughMounts` (FUSE, root: a cold start with its
  trace makes exactly two index and three frame requests, and the same reads
  then wait for no store), `TestStartupTracesReachTheServerOnceReady`,
  `TestSharedStartsReportNoTrace`, `TestFailedStartsStopTheirPrefetch`
  (agent), `TestStartupTracesStayWithTheirWorkspace` (hostsession, Postgres:
  the next start in the workspace carries the trace, another workspace's
  start does not, invalid and foreign traces are dropped). CI on #494 green,
  host-runtime included.
- EC2, us-east-2a, m7i.large from ami-0e3303f49502924b3, 60 GiB gp3, layers
  converted into a scratch S3 bucket and read through presigned GETs,
  manifests from a registry:3 on the host. Each run: image removed,
  snapshotter restarted (empty cache), page cache dropped; pull plus one
  `docker run --runtime runsc`. Median of 3, seconds:

| Path | Image | Pull | Run | Total | Bytes fetched |
| --- | --- | --- | --- | --- | --- |
| snapshotter packet, lazy S3 | python:3.12-slim | 0.40 | 1.85 | 2.32 | 42 MB |
| no trace | python:3.12-slim | 0.30 | 1.92 | 2.23 | 42.1 MB, 32 frames |
| with trace (20 frames) | python:3.12-slim | 0.31 | 1.99 | 2.32 | 42.1 MB, 32 frames |
| snapshotter packet, lazy S3 | torchserve 0.12.0-cpu | 0.65 | 12.54 | 13.28 | 134 MB |
| no trace | torchserve 0.12.0-cpu | 0.58 | 12.26 | 12.82 | 134.7 MB, 117 frames |
| with trace (104 frames) | torchserve 0.12.0-cpu | 0.57 | 6.60 | 7.18 | 134.7 MB, 117 frames |
| every frame already cached | torchserve 0.12.0-cpu | 0.40 | 5.15 | 5.55 | 0 |

- Prefetch reads no more: with a trace both images fetched the same bytes
  and frames as without, to the byte. The torch prefetch of 101 frames ended
  2.0 to 2.4 s after the layers mounted. Python gains nothing: its layers
  are under `--fill-bytes` and fetched whole anyway.
- With every frame on local disk torch still takes 5.55 s, so prefetch
  leaves 1.6 s of the 7.3 s store wait; the rest of the gap to a warm run
  (2.7 s) is FUSE and the page cache, not the store.
- What did not help: 32 fetch slots instead of 16 (6.76 s), and keeping
  an idle store connection per fetch slot instead of Go's default two
  (7.11 s). Neither is kept.

## Intentional differences

- Traces are kept per workspace and reference, not per reference: what a
  container reads says something of its code, so a trace never reaches
  another workspace's host, and a host can only skew its own workspace's
  prefetches. Each workspace's first cold start of an image goes untraced.

## Gaps and unverified boundaries

- The EC2 runs drove the snapshotter with a scratch harness, not the agent
  and server: the agent path is covered by the owner tests, not timed.
- Prefetch starts when the layers mount, at container create; starting at
  pull, before the mount, could save up to the pull's 0.6 s.
- A trace on a host already running a container of one of the image's
  layers is incomplete and not reported, so busy hosts record no traces.
- GPU images and ECR are untried.
