# Acceptance benchmarks: reference 9e259ce75 against go-rewrite b472ade9a

The harness is in `acceptance/bench/` and the raw records in
`acceptance/bench/results/`. `report.py` rebuilds the tables below from them.

## Conditions

- One host: 24 CPUs, 62 GiB, Ubuntu 24.04, kernel 7.0.0, Docker 29.6.2 with
  runc, cgroup v2. Other developers' stacks ran on the same host throughout
  (load average 1 to 2). The two stacks ran one at a time; the idle one stayed up.
- PostgreSQL 18-alpine in both, Compose defaults (fsync on, durable WAL),
  with `pg_stat_statements` preloaded. The reference reaches it through
  PgBouncer and also runs Redis 8.
- Each platform got one host offering 4 CPUs and 8 GiB. The reference agent is
  its Compose `customer-compute` machine, so the workloads pin
  `machine="lcbench-agent"`. The rewrite agent is the `run.sh` platform host.
  Both accounts are complimentary (Team limits in the reference, Business in
  the rewrite); no limit was hit.
- Topology is each platform's default. Reference: control-plane, execution-api
  and runtime-api (2 workers each), 2 schedulers, 2 fleet controllers, 2
  connection gateways, HAProxy ingresses, cache server, OTel collector, one
  always-on worker container. Rewrite: `server`, `scheduler` and `agent` host
  processes, with Garage and a registry.
- Same `benchapp.py` and the same call patterns (`lazycloud deploy`, `.remote()`,
  `spawn_map` and `get`, httpx against endpoint URLs), each through that
  tree's own SDK. `Image(python_version="3.12")` builds a Debian image once in
  the reference (27 s); the rewrite uses `python:3.12-slim` with its mounted
  runtime and builds nothing.
- CPU is cgroup or /proc time over the window, memory is anonymous or resident
  bytes, PG bytes are the PostgreSQL container's network bytes.

## Results (p50 / p95 / p99)

| Scenario | Reference | Rewrite |
| --- | --- | --- |
| Deploy, image cached, ms | 1276 / 1351 / 1351 | 722 / 1219 / 1219 |
| Cold `.remote()`, ms | 1318 / 1425 / 1425 | 1226 / 2109 / 2109 |
| Cold: admission to demand / placement / container ready / execution, ms | 32 / 32 / 722 / 26 | 3 / 6 / 1081 / 6 |
| Warm `.remote()`, ms | 118 / 141 / 249 | 15 / 18 / 21 |
| map 200: admitted/s, total s | 47, 12.1 | 1971, 3.2 |
| map 2000: admitted/s, total s, queue wait p50 | 63, 101.3, 12.1 s | 7775, 5.0, 0.8 s |
| map 10000: server span, queue wait p99 | 272 s, 93 s (SDK client hung, below) | 17.7 s, 16.7 s; total 31.7 s |
| map 2000 DB time per task | 7.1 ms | 3.3 ms |
| map 10000 DB time per task | not sampled | 14.9 ms (PostgreSQL at 6.4 cores) |
| No capacity, 2 workspaces x 1000: admitted/s per workspace | 35, 35 | 6464, 7260 |
| Capacity restored: first start, drain, start rate | 49.5 s, 670 s, 3.2/s | 2.0 s, 12.2 s, 195/s |
| Backlog age at start, s | 601 / 690 / 698 | 39 / 42 / 43 |
| First quarter of starts by workspace | 0.00 / 1.00 | 0.48 / 0.52 |
| Warm endpoint, ms | 129 / 154 / 258 | 1.6 / 2.2 / 2.6 |
| Cold endpoint, ms | 903 / 993 / 993 | 1124 / 1635 / 1635 |
| SSE first event, ms | 306 / 411 / 444 | 1.8 / 2.9 / 4.5 |
| SSE gap between events sent 50 ms apart, ms | 0 / 256 / 263 | 50 / 51 / 52 |
| SSE, 20 streams at once, total (ideal 1000), ms | 1506 / 1704 / 1782 | 1033 / 1043 / 1046 |
| 1000 concurrent requests, rounds 1/2/3: wall s | 24.5 / 24.7 / 25.1 | 14.9 / 6.1 / 1.8 |
| 1000 concurrent, round 3 latency, ms | 15443 / 22437 / 23334 | 877 / 1607 / 1692 |
| 1000 concurrent failures, all rounds | 38 x 503, 13 x 500 | 0 |
| Idle CPU, memory | 40% of a core, 3336 MiB | 3.6%, 504 MiB |
| Idle PG statements/s, transactions/s, bytes/s | 108, 61, 114 kB | 31, 14, 9 kB |
| Idle PG rows returned/s | 211 | 1096 to 3321 |
| Idle Redis commands/s | 90 | none |
| Idle PG statements/s, 1 then 2 schedulers | 108, 108 | 31, 61 |
| Idle with 110k (ref) / 115k (rewrite) finished tasks: CPU, PG statements/s | 41%, 107 | 3.7%, 31 |
| map 2000 from cold after idle: 2 schedulers / 100k history, total s | 101.8 / 102.4 | 10.8 / 10.6 |

Five cold calls, 150 warm calls, 1000 warm requests, 5 cold endpoints, 50 and
200 SSE streams. Each cold `.remote()` uploads new source, so it needs a new
container.

## Regressions in the rewrite

1. **The dependency check scans the queued backlog on every completion.**
   `select t.id from tasks t where t.status = $2 and t.id in (select ...
   task_dependencies ...) for update` runs once per finished task. With 10,000
   queued it walks `tasks_workload_queued` and filters, about 10,000 blocks
   and 27 ms a call. In a repeat 10k map it took 269 s of about 305 s of
   statement time. PostgreSQL used 6.4 cores during the measured run. DB time per task grows from
   3.3 ms at 2,000 inputs to 15 ms at 10,000, so cost is quadratic in backlog
   and the database is the next saturation point. Server throughput fell from
   1,240 tasks/s at 2,000 inputs to 565/s at 10,000. Skip the query when the
   task has no dependents, or drive it from `task_dependencies`.
2. **Container readiness is slower.** Assignment to ready takes 1.08 s
   against 0.72 s, so cold `.remote()` p95 and cold endpoints (1124 against 903
   ms) are worse even though admission and placement are 5 to 10 times faster.
   The reference starts user processes inside a running worker; the rewrite
   creates a Docker container per workload.
3. **Scheduler replicas multiply polling.** Two replicas double idle
   statements (31 to 61/s) and bytes. The reference stays at 108 with either
   count. A second replica did not speed up a map.
4. **Idle work scans retained container rows.** At idle the `containers` table
   gets 6 sequential scans a second that read every row, stopped ones
   included (93 rows here). It grows with container history, not live work,
   and finished-task history did not show it.
5. The first 1000-request burst on a cold endpoint takes 14.9 s while the
   autoscaler adds containers. That is better than the reference (24.5 s with
   44 errors), but it is the slowest rewrite path.

## Performance fixes (branch perf-fixes)

Before is go-rewrite at dd0071467 and after is perf-fixes merged with
568ebaf0e. Both ran on the same host and stack, fresh each time, through the
same sequence (deploy, remote, maps, replicas). The raw files are
`results/new-before.jsonl` and `results/new-after-same-sequence.jsonl`. The
full suite against the reference is `results/report-after.md`.

**1. Per-completion statements walked the queued backlog.** Statistics
taken while the queue is empty make the planner treat the queued partial
indexes as free and walk them. The dependency locks now read tasks by key.
`OFFSET 0` keeps the status test out of the locking scan, and terminal
dependents are locked and then dropped. The release batch reads the first
rows an index yields. Planning finds queued releases with a skip scan and
counts demand only up to `max_containers * tasks_per_container`.

| At 10,000 queued, statistics from before the queue filled | Before | After |
| --- | --- | --- |
| LockQueuedDependents, buffers | 191 | 2 |
| LockDependentClosure, buffers | 182 | 8 |
| LockQueuedWithDependents (batch of 10), buffers | 12,639 | 30 |
| PlanningReleases queued candidates, buffers | 132 | 7 |
| map 10,000: DB blocks per task | 2,416 | 567 |
| map 2,000: DB blocks per task | 984 | 555 |

Server span for the 10,000-input map did not change beyond noise (14.8 s
before, 7.4 to 15.4 s across the after runs). The remaining per-claim cost is
the claim query passing dead `tasks_queued` entries under fast churn. That is
about 300 buffers a claim late in a 10,000-input map, and it grows with
churn, not with the backlog.

**2. Container readiness.** The managed runtime shipped without bytecode and
is mounted read-only, so every start compiled every module it imported. It
now ships unchecked-hash bytecode. The runner imports uvicorn only for HTTP
workers. The generated API models build on first use (`APIModel`,
`defer_build`).

| | Before | After | Reference |
| --- | --- | --- | --- |
| Runner and app import, ms | 950 | 540 | |
| Runtime stage (start to ready), ms | 941 | 535 to 600 | |
| Assigned to ready, median ms | 1,109 | 644 to 730 (3 runs) | 722 |
| Cold `.remote()` p50 / p95, ms | 1,278 / 1,343 | 866 / 1,054 | 1,318 / 1,425 |
| Cold endpoint p50, ms | 1,124 | 780 | 903 |

Docker create and start takes 95 to 340 ms of that and varies with the
host's other containers.

**3. Idle polling grew with scheduler replicas.** One replica leads through
a session advisory lock, and the others block inside PostgreSQL waiting for
it. Only the leader runs timed passes. While no task is queued or running
and no container is live, quiet loops wait for a NOTIFY or a 30 s safety
tick. Schedules and callbacks sleep until their next due time, and a schedule
change notifies `lc_schedule`. Every replica still runs passes when woken.

| Idle, whole stack | Before | After | Reference |
| --- | --- | --- | --- |
| PG statements/s, 1 scheduler | 46.4 | 7.9 | 108 |
| PG statements/s, 2 schedulers | 90.8 | 7.8 | 108 |
| PG bytes/s, 1 / 2 schedulers | 11.7k / 25.0k | 3.6k / 3.1k | 112k / 114k |
| Scheduler passes/s, 1 / 2 replicas (guard; before is the old timing) | 6.0 / 12.0 | 0.17 / 0.17 | |

**4. Idle reads of retained rows.** With 20,042 stopped containers no idle
statement scans `containers` whole, before or after. The 93-row scans in the
first run were the planner reading a tiny table. Three real history reads
are fixed:
- Each host report read every attempt that host's live containers finished in
  the last ten minutes. It now reads the `attempts_ended_unseen` partial
  index.
- The live-work probe uses ordered partial-index lookups. EXISTS let the
  planner scan history for a match.
- The planning skip scan above.

Idle statements fell from 49.6 to 8.0/s at that history. Idle rows read
match the reference within noise: 143 to 221/s against 200 to 221/s.

**Guards** run against real PostgreSQL in CI time:
- `TestPerCompletionCostIgnoresBacklog` (internal/execution, 2 s) runs EXPLAIN
  ANALYZE on ten per-completion and claim statements at 100 and 10,000 queued
  over 20,000 finished tasks. It checks fresh and stale statistics and custom
  and generic plans. It fails if buffers grow by more than 20 or a node over
  tasks, attempts or containers reads more than 500 rows. It fails on the
  pre-fix queries.
- `TestRecurringScansReadOnlyLiveRows` (internal/execution, 1 s) checks the
  same bounds for fifteen recurring idle statements between 1,000 and 21,000
  finished tasks.
- `TestIdlePassesDoNotGrowWithReplicas` (cmd/scheduler, 17 s) runs two real
  schedulers. It fails above 1.5 idle passes/s or if the second replica adds
  more than 0.5/s. Per-replica polling measured 6 and 12.
- `TestLeadElectsOneAndHandsOver` (internal/database, 3 s) covers election,
  session loss and handover.

Against the reference after the fixes, the rewrite is ahead in every measured
scenario except idle rows read, which is level within noise
(`results/report-after.md`). Assigned to ready ranged 644 to 730 ms across
three runs against the reference's single 722 ms measurement.

## Recheck after the parity PRs (branch perf-recheck)

The full suite ran again on go-rewrite 11ceab3fd, which includes #443 to
#448, on the same host and stack as before. A new scenario sends requests to
an endpoint whose release has a `callback_url` and times each callback at a
local receiver. Raw files are `results/recheck-*.jsonl`.

| | post-#442 | 11ceab3fd | perf-recheck | Reference |
| --- | --- | --- | --- | --- |
| Cold `.remote()` p50 / p95, ms | 866 / 1,054 | 869 / 1,139 | 845 / 886 | 1,318 / 1,425 |
| Assigned to ready, median ms | 677 | 717 | 659 | 722 |
| Warm `.remote()` p50 / p95, ms | 12.9 / 16.8 | 15.5 / 33.7 | 12.5 / 16.5 | 118 / 141 |
| map 2,000: total / server span, s | 4.1 / 1.5 | 4.3 / 1.5 | 5.3 / 2.4 | 101 / 52 |
| map 10,000: total / server span, s | 27.0 / 14.6 | 22.5 / 9.6 | 26.5 / 7.8 | client hung / 272 |
| Endpoint warm p50 / p95, ms | 1.3 / 1.7 | 1.2 / 1.5 | 1.2 / 1.5 | 129 / 154 |
| Endpoint cold p50 / p95, ms | 780 / 791 | 768 / 880 | 758 / 782 | 903 / 993 |
| SSE, 20 at once, total p50, ms (ideal 1000) | 1,031 | 1,030 | 1,031 | 1,506 |
| 1000 concurrent, third round wall, s | 2.6 | 1.8 | 1.9 | 25.1 |
| Idle PG statements/s, 1 / 2 schedulers | 8.6 / 8.7 | 9.0 / 8.7 | 8.9 / 8.7 | 108 / 108 |
| Idle with 100k finished tasks, PG statements/s | 8.6 | 9.1 | 9.0 | 107 |
| map 2,000 over 100k finished tasks, server span, s | 3.3 | 3.7 | 3.2 | 54 |

Warm `.remote()`, readiness and the 2,000-input map moved both ways between
runs. To separate code from host load, the post-#442 tree (bc22f4d86) and
11ceab3fd ran back to back on fresh stacks with the same calls
(`results/recheck-ab-*.jsonl`). The two trees matched within noise:
- warm p50 15.4 to 16.5 ms against 14.1 to 16.4 ms;
- assigned to ready median 732 against 744 ms, with 694 to 818 and 654 to 996 across the ten runs each;
- server-side queue and execution 3.35 and 3.98 ms against 2.90 and 3.49 ms.

Repeated warm 2,000-input maps took 1.43 to 1.54 s server span, and maps on
cold containers with nothing else running took 3.1 s. Neither the code nor
the post-#442 numbers changed; the host did.

Idle statements are about 0.3/s higher, from upkeep the parity PRs added:
callback purge on its own minute loop and the metering session-lock check.
They stay flat with replicas. Idle rows read
moved between 160 and 247/s against the reference's 200 to 221/s. The
excess at small tables is the planner scanning a few-dozen-row table whole.
At 214,000 metric samples the rollup reads only its window through
`container_metric_samples_age`.

**Callbacks were slow to leave.** A request callback waited for the edge's
one-second record flush, then for the deliverer's one-second tick. The edge
now writes a record that owes a callback within 50 ms and wakes delivery
with `lc_callback` (`database.ChannelCallback`) in the same transaction.
Task callbacks wake it too.

| Endpoint with callback_url | 11ceab3fd | perf-recheck | Reference |
| --- | --- | --- | --- |
| 200 requests, 10 at once: request p50 / p95, ms | 10 / 32 | 14 / 37 | 386 / 538 |
| Callback after the response, p50 / p95, ms | 866 / 949 | 58 / 95 | -51 / -47 |
| Callback after the request started, p50 / p95, ms | 879 / 964 | 75 / 110 | 334 / 427 |
| 1000 requests, 100 at once: request p50 / p95, ms | 152 / 611 | 164 / 681 | 2,762 / 4,185 |
| Callback after the response, p50 / p95, ms | 1,319 / 1,886 | 26 / 50 | -54 / -19 |
| Callback after the request started, p50 / p95, ms | 1,560 / 2,153 | 190 / 700 | 2,291 / 2,823 |

None of the three missed or duplicated a callback. The reference calls
back inline before it answers, so its callbacks precede the response and its
requests pay for delivery. The rewrite now beats it from request start to
callback at both loads. Reference responses carry no request id, so its
callbacks are paired with responses in arrival order. Measuring needed two
changes to the reference copy, like the label change above: it allows a
private callback target, which it otherwise refuses. The receiver's listen
backlog went to 1,024, because the default of five dropped connections and
added a one-second retransmit to every system's numbers.

Guards: `TestARequestOwingACallbackIsQueuedPromptly` (internal/edge, under
1 s) fails with the one-second flush. `TestQueuedCallbacksWakeAnIdleScheduler`
(cmd/scheduler, 3 s) runs a real idle scheduler. It fails if a queued
callback waits for the delivery loop's safety tick; delivery took 15 ms.

## Reference defects found

- After an agent stop and start with a container stopped mid-cleanup, the
  worker crash-looped for over 10 minutes on `container storage cleanup remains
  incomplete` and accepted work never ran. I set that container's
  `storage_released_at` by hand to continue.
- The 10,000-input map finished on the server in 272 s, but the SDK client
  held 4,093 open API connections and stopped making progress. I killed it
  after 14 minutes and took the record from server rows.
- With capacity back, one workspace's 1,000 tasks all ran before the other
  workspace started (first-quarter share 0 / 1).
- The first SSE request to a cold ASGI app failed three of five streams with
  180 s timeouts in a smoke run. Warm streams batch events about 250 ms apart.

## Not measured fairly

- Image preparation: the reference builds an image, the rewrite mounts a
  runtime into a stock image, so first-deploy times are not comparable. The
  deploy row uses a cached image in both.
- Capacity acquisition: only local hosts exist, so provider launch and agent
  startup are not covered.
- Reference platform CPU during maps excludes its worker container, which also
  runs the user code. The rewrite counts each workload container separately.
- History was grown by cloning succeeded tasks and their final attempts (plus
  inputs and results in the rewrite) with SQL, not by running 100k tasks. Logs
  were not cloned.
- One run per scenario on a shared host, so treat differences under 20% as
  noise.

## Handwritten production code

Reference 9e259ce75 against go-rewrite after the final sweep (branch
final-sweep), each counted from `git archive` of its commit. Lines are cloc
code lines, so blanks, comments and docstrings are out. Tests, generated files
(sqlc, oapi-codegen, protobuf, datamodel-codegen, openapi-typescript, the
TanStack route tree) and lockfiles are counted apart. SQL queries, the schema,
YAML contracts, Helm, Terraform and shell count as handwritten.

| Capability | Reference | Rewrite | Rewrite generated | Why |
| --- | --- | --- | --- | --- |
| Functions and execution | 22,200 | 6,676 | 7,040 | Admission, attempts, retries, results, apps, deployments, schedules and callbacks are SQL in two owners. The reference spread them over execution, control, operations and scheduler autoscaling, plus 5,140 lines of repository classes. |
| Scheduling | 7,413 | 235 | 170 | Placement is one SQL pass over capacity rows. The reference kept placement state and capacity reservations in Redis (`state.py` alone is 3,514). Part of the decision now sits in execution planning and compute capacity, which their rows count. |
| Compute and fleet | 36,966 | 5,483 | 2,729 | One EC2 launcher and a capacity controller replace managed and retained pools, reserve planning, demand forecasts and a fleet simulator. The AWS provider alone was 8,715 and its repositories 4,999. |
| Images | 6,548 | 2,603 | 645 | Dockerfile rendering, image identity and registry publication. Builds run on hosts with BuildKit and count under host runtime (`agent/build.go`, 683). |
| Storage | 15,703 | 3,509 | 1,870 | The AWS SDK replaces a hand-written S3 client (1,093); there is no cache server (1,445) and no repository layer (3,675). |
| Endpoints and edge | 6,340 | 4,760 | 806 | The smallest cut. The edge forwards HTTP and WebSocket streams over host data streams, relays between servers, and owns request logs and Cloudflare domains. The reference did part of that forwarding in its connection gateway, which counts under host runtime. |
| Workloads (pods, devboxes, sandboxes, shells) | 6,034 | 3,073 | 0 | Pods reuse the container lifecycle, and SSH and shells end in the supervisor. Pod port and TCP proxying count here on both sides. |
| Billing and usage | 9,455 | 5,197 | 2,819 | Ledger, rate cards, metering and Stripe flows stay detailed. SQL replaces 3,364 lines of repository code. |
| Identity and secrets | 7,266 | 3,324 | 1,621 | Sign-in, tokens, device login, invitations and sealed secrets in two packages. The reference repositories alone were 1,870. |
| Observability and notifications | 5,981 | 3,542 | 1,374 | Triggers and NOTIFY feed the change stream and SQL folds metrics. The reference kept stream state in Redis (1,004). |
| Host runtime (agent, supervisor, disks, runner) | 60,614 | 18,159 | 8,710 | One Go agent, one supervisor and the disk engine replace the Python worker (28,684), the connection gateway (5,840), the worker repository API and three Go helpers. The host protocol is the gRPC session in `hostsession`. |
| SDK and CLI | 25,090 | 21,274 | 0 | Same public API and commands. Clients build on generated models. Bundled examples are out on both sides. |
| Contracts and shared Python models | 14,757 | 10,439 | 2,691 | OpenAPI and Protobuf (8,552) are the one source, and 1,867 lines of shared Python remain. The reference shared package served both the backend and the SDK. |
| Web | 38,381 | 35,622 | 10,229 | Same pages and styling. Generated openapi-fetch types replace 2,009 lines of zod schemas. |
| Platform core (API plumbing, database, processes) | 18,043 | 3,619 | 21,600 | Binaries, the schema (1,337 lines of SQL) and pool, listener and leader helpers. The reference had SQLAlchemy tables and mappers (8,219), FastAPI composition and services (4,849), Redis coordination (1,927) and a scheduler app. Most of the generated count is the oapi-codegen strict server. |
| Deploy and infra | 10,422 | 4,159 | 0 | Rewrite `deploy/` and `.github` against the reference `deploy/`, `docker/` and `.github`. Helm, Terraform and Argo CD replace 6,334 lines of Python release, AMI and chart tooling. |
| Backend and host (all rows but SDK, contracts, web and deploy) | 202,563 | 60,180 | 49,384 | |

How the reference maps:

- `apps/api` routers go to the capability they serve. The worker repository
  service and gateway routers go to host runtime, and app, dependency and
  service composition to platform core.
- `packages/database/repositories` splits by file name: `billing_*` to
  billing, `capacity_*`, `compute` and `worker_releases` to compute,
  `container_scheduling` to scheduling, and so on. Tables, mappers and records
  are platform core.
- `packages/execution` sends `endpoints/` to the edge, `pods/`, `shells/` and
  `ssh/` to workloads, `volumes/`, `artifacts/` and `collections/` to storage,
  and `secrets/` to identity. `packages/control` sends sandboxes to workloads,
  custom domains and TCP ingress to the edge, and workspaces to identity.
  `packages/operations` (cross-owner management services) is execution.
- `packages/scheduler` sends autoscaling, containers, orphan recovery and cron
  to execution, pools, workers and fleet to compute, and the rest to scheduling.
- `packages/compute` and `packages/providers/aws` go to compute, except bucket,
  block volume and workspace storage files (storage), request placement
  (scheduling), tunnel authority (edge) and supplier costs (billing).
- Stripe goes to billing, Resend to notifications, GitHub to identity, and
  Cloudflare and `packages/networking` to the edge. `packages/gateway`,
  `worker`, `worker-repository`, `agent`, `runner` and the Go helpers in `apps/`
  go to host runtime.
- Left out: the unused admin CLI (`apps/cli`, 4,548), Alembic history (2,287),
  bundled examples (1,976 here, 1,961 in the rewrite), benchmarks and the
  Compose files on both sides.

The rewrite maps by owner package, and `internal/api` splits by file the same
way. Execution's pod, devbox, sandbox, snapshot and SSH files go to workloads,
its image builds to images, endpoint serving to the edge and log storage to
observability. `edge/pods.go` and `edge/tcp.go` go to workloads.

Tests are 104,268 lines in the reference plus 3,761 in web, and 36,891 in the
rewrite (with the acceptance harness) plus 4,526 in web. Generated code is
52,075 lines in the rewrite plus 10,229 in web, against 525 in the reference
web.

The final sweep removed 6,775 handwritten lines, 1,036 test lines and 1,650
generated lines net: shared Python modules and definitions only the reference
backend read, the admin CLI extension hooks, unused SDK client methods,
test-only Go accessors, duplicate helpers, and the `listUsers`,
`listSchedules` and `getTaskTimeline` operations, which nothing called, with
their handlers, queries and schemas.
