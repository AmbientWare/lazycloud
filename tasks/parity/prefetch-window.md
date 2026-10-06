# Prefetch window

## Scope

Startup traces end when the container reports ready (`onReady` to
`layers.ready` in `internal/agent/container.go`), but a handler's imports
(`import torch` in the benchmark) run after that, inside the first task. So
the frames they read are never traced or prefetched: torch cold starts took
7.4 to 7.7 s in the import on hosts without the image, 2.7 s with it
cached. Owns the snapshotter's trace window and the agent's trace start and
end.

## Plan

- A trace covers the container's start through the end of its first task
  or request, bounded in time (choose the bound from the measurements), so
  imports in the handler are recorded.
- Keep the review's rules: a trace any other start shares a layer with is
  incomplete and never reported; traces stay per workspace and image.

## Evidence to record

- Torch cold start from S3 on a fresh EC2 host with a trace recorded this
  way, three runs, against today's 9.4 s median; bytes fetched with and
  without.

## Progress

## Gaps and unverified boundaries
