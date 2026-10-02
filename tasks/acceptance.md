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
  the last ten minutes. It now reads the `attempts_ended_unseen` partial index
  (migration 0013).
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

Lines from cloc without blanks, comments or docstrings; tests and generated
files are excluded and listed separately. The rewrite has not reached parity
yet (every area in update.md is unchecked), so these figures do not show a
simplification.

| Capability | Reference | Rewrite | Rewrite generated |
| --- | --- | --- | --- |
| API and transport | 19,706 | 4,815 | 18,446 |
| Execution | 13,312 | 4,192 | 3,573 |
| Scheduling | 17,402 | 975 | 170 |
| Compute and fleet | 32,580 | 4,768 | 2,729 |
| Host runtime | 42,188 | 9,284 | 7,006 |
| Storage and cache | 12,348 | 5,654 | 1,850 |
| Images | 5,858 | 1,876 | 608 |
| Billing | 3,634 | 4,702 | 2,679 |
| Identity and secrets | 3,495 | 2,725 | 1,636 |
| Control and schedules | 4,069 | 1,831 | 1,141 |
| Networking and gateway | 7,969 | 3,371 | 802 |
| Observability and notifications | 3,923 | 2,674 | 1,433 |
| Database access and schema | 30,149 | 1,454 | 0 |
| Operations and deploy | 10,447 | 1,177 | 0 |
| Backend total | 207,800 | 49,498 | 42,073 |
| Contract sources (OpenAPI, Protobuf) | none | 7,202 | |
| Python SDK and CLI / runner / shared | 26,914 / 2,320 / 14,737 | 22,981 / 957 / 6,762 | |
| Backend tests | 76,460 | 20,954 | |

The reference also has 4,513 lines of unused admin CLI and 2,344 lines of
Alembic history. The web app is unchanged at 35,097 lines.
