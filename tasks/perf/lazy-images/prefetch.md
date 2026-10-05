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

## Intentional differences

## Gaps and unverified boundaries
