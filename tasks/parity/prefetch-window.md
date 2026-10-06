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

Done on 2026-10-06 on `parity-prefetch-window`, draft PR #501 into
`parity-plan`; CI green, host-runtime included.

- Agent (`internal/agent/layers.go`, `container.go`, `data.go`, `pod.go`):
  a start that traces waits in one owned goroutine for the end of its first
  task (`onFinished`), HTTP or port request, or pod command, at most
  `traceWindow` (60 s) after the trace began, then ends and reports the
  trace under the same rules as before. An exit wakes the wait and reports
  nothing. Ready no longer ends the trace. The snapshotter is unchanged: its
  15-minute trace life stays the backstop for traces an agent restart
  orphans.
- Contract comments on `StartupTrace`, `record_trace` and `prefetch`
  describe the new window; bindings regenerated.
- Tests: `TestStartupTracesEndWithTheFirstTask` (the trace still runs after
  ready and is reported once the first task completes),
  `TestStartupTracesEndWithinTheWindow` (an idle container reports at the
  window; an exit ends the wait at once with no report),
  `TestSharedStartsReportNoTrace` kept.

## Evidence

EC2 us-east-2a, m7i.large from the fleet CPU image ami-0104d7b9500423a78,
pytorch/torchserve:0.12.0-cpu converted with `internal/imagefs` into a
scratch S3 bucket, presigned GETs, manifests from a local registry. A
scratch harness drove the snapshotter as the agent does: grant, optional
prefetch, trace, lazy pull, `docker run --runtime runsc` of a script that
prints READY, then imports torch and runs a matmul (the first task). Each
run: image removed, snapshotter restarted, page cache dropped. Seconds from
the grant to the first task's end:

| Trace | Frames | Runs | Median | Fetched |
| --- | --- | --- | --- | --- |
| none | 0 | 10.75, 10.71, 11.70 | 10.75 | 135.3 MB, 119 frames |
| ended at ready (before) | 22 | 9.96, 9.45, 9.79 | 9.79 | 135.3 MB, 119 frames |
| ended with the first task | 105 | 4.44, 4.47, 4.46 | 4.46 | 135.3 MB, 119 frames |

The recording starts ended their first task 10.3 and 11.5 s after the
trace began; the 60 s window leaves five times that for larger images.
Prefetch fetched no extra bytes.

## Gaps and unverified boundaries

- The EC2 runs drove the snapshotter with a harness, not the agent and
  server; the agent's window is covered by the owner tests. Prod acceptance
  should confirm against the 9.4 s benchmark median.
- A longer window makes overlap with another start on the same host more
  likely, which leaves both traces unreported; scale-up waves of one image
  onto one host record nothing until a later lone start.
- GPU images are untried; their imports may approach the window.
