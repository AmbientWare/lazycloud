# Performance packets: batch task wait and lazy images

These files live on `perf-plan`, the integration branch. They never merge to
main: the final PR from `perf-plan` removes `tasks/perf` first.

The evidence is the 2026-10-05 prod benchmark against beam.cloud (scripts in
`/tmp/lcb2`, no secrets):

- A 100-item `map()` took 19 s. The server spent about 47 ms per task; the
  SDK spent the rest waiting for each result with its own round trips
  (`GetTask` with `wait_seconds`, then `GetTaskResult`).
- The first container on a fresh host spent 2.45 s pulling a plain Python
  image, half of a 5.1 s first call. Images with torch or CUDA take tens of
  seconds. Hosts do a full `docker pull` today.

## Decisions

The user decided on 2026-10-05:

- Build the batch task wait and CLIP-style lazy images now. The runtime
  start profile, the workload edge path and new CPU types wait.
- Lazy images keep Docker. Our own layer store serves layers through a
  containerd snapshotter, so the work also runs under plain containerd later.
  No dependency on SOCI or ECR-specific features; it runs the same locally
  (registry, Garage) and in prod (ECR, S3).
- Hard cuts: no compatibility paths. Once lazy images ship, every image a
  host runs comes through the snapshotter.

Integrator defaults, taken unless the user objects:

- A host reads only the layers of images it was told to run. The server
  signs those reads; no host, including a connected account's, holds a
  credential for the whole layer store.
- The peer-to-peer frame cache is planned but waits for the acceptance
  numbers.

The spike answered yes to all four questions on 2026-10-05 (spike.md).
Its findings set the design in lazy-images/plan.md: pull through
containerd's API, never restart the process holding FUSE while containers
run, convert every image, and store each layer as an index plus one framed
data object read by range with presigned URLs.

## Packets

| Packet | Branch | Migration | Shared-file range | Depends on |
| --- | --- | --- | --- | --- |
| [batch-wait](batch-wait.md) | `perf-batch-wait` | none | OpenAPI `/v1/workspaces/{workspace}/tasks/wait` | none |
| [spike](lazy-images/spike.md) | `perf-lazy-spike` | none | none (scratch only) | none |
| [format](lazy-images/format.md) | `perf-lazy-format` | none | new `internal/imagefs`, `contracts/imagefs/v1` | spike |
| [publish](lazy-images/publish.md) | `perf-lazy-publish` | 0003 | host.proto fields 90-99 in each message it extends | format |
| [snapshotter](lazy-images/snapshotter.md) | `perf-lazy-snapshotter` | none | snapshotter service, the agent's image pull, node image | format |
| [grants](lazy-images/grants.md) | `perf-lazy-grants` | none | host.proto fields 100-109 | format |
| [acceptance](lazy-images/acceptance.md) | `perf-lazy-acceptance` | none | none | all |

The lazy images design is [lazy-images/plan.md](lazy-images/plan.md).

## Waves

1. Wave 1, in parallel: batch-wait and spike. Batch wait merges to main on
   its own once it passes the gate; it does not wait for lazy images.
2. Wave 2: format first. Then publish, snapshotter and grants in parallel
   on top of it, each against format's contract and the host protocol
   ranges above. Grants and snapshotter agree the local interface between
   agent and snapshotter in their first commits (grants proposes, the
   integrator settles it).
3. Wave 3: acceptance, then the integrator decides on the peer cache from
   its numbers.
4. Final: one integration review of `perf-plan` against main, remove
   `tasks/perf`, one PR, one Ship with the user's go-ahead, then the
   acceptance packet's prod steps.

## Agent protocol

- One agent per packet, each in its own worktree on its own branch from
  `perf-plan`. No sub-agents.
- Read AGENTS.md, this file, the packet file and the plan before any work.
- Own only the files the packet names. Propose shared changes in the report;
  the integrator makes them and tells the others.
- Local stacks use their own compose project, ports and state directory and
  come down afterwards. Never touch containers, stacks or branches you did
  not create, never stop shared services, never stop the shared test
  Postgres (`compose.test.yaml`, port 15432).
- Real EC2 runs in `AWS_PROFILE=default`, US regions only, tagged
  `lazycloud:task=<packet>`, terminated before the report.
- Commit and push after each meaningful step, one-line subjects, no
  attribution trailers. Don't open PRs.
- Record progress, evidence and intentional differences in the packet file.

## Merge gate

Each packet passes this before it merges into `perf-plan` (or, for
batch-wait, into main):

1. `./check.sh` and focused `go test -race` on the touched packages pass with
   visible output; generated code is current and the tree is clean after.
2. A separate read-only reviewer gets the diff and this focus list:
   authorization and tenant isolation (who may read which layers, which
   tasks); data integrity and idempotency on retry; goroutine, FUSE server
   and mount lifetimes across agent restarts; queries that scan history or
   the backlog; bounded queues, caches and disk use; dead code and contract
   drift. Only verified defects with a file, line, failure scenario and fix.
3. Fix every real finding with a test that fails without the fix.
4. Green means green: every check passed, none pending. Squash with a
   one-line subject and a one- or two-line description.

The final PR to main also gets a full review, dead and repeated code
removal, condensed prose and an unslop pass.
