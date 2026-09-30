# Cron admission evidence

Measured locally on September 30, 2026, against baseline `41f2ade14` and the
implementation in `2916b4768`. Both used the same machine, PostgreSQL 18 and
Redis 8. The history measurements below include the corrected public `workspace`
query parameter on both revisions.

## Admission, independent of workers

Run `uv run --group dev pytest -x -s -q benchmarks/cron_admission.py` with
`docker compose -f compose.test.yaml up -d --wait` running.

The benchmark invokes the production cron and function owners against isolated
PostgreSQL and Redis. Workers do not execute tasks. Time includes preparing and
committing tasks, recording occurrences, advancing schedules and publishing
notifications. The fixture supplies storage adapters; it does not prove image
builds, provider provisioning or worker execution.

Each size has one warmup and 12 measured ticks. Every ten schedules use a separate
account and workspace. Admission, billing eligibility and queue limits remain
enabled. All measured occurrences must produce tasks. SQL bytes are PostgreSQL
result field bytes, excluding protocol framing, outgoing writes and Redis traffic.

| Due schedules | Statements before / after | Returned bytes before / after | Median ms before / after | p95 ms before / after |
| --- | --- | --- | --- | --- |
| 0 | 2 / 2 | 0 / 0 | 0.62 / 1.09 | 1.62 / 1.36 |
| 1 | 29 / 26 | 7,806 / 6,425 | 29.44 / 26.31 | 35.20 / 34.42 |
| 10 | 272 / 242 | 78,052 / 64,267 | 267.85 / 238.48 | 277.86 / 362.97 |
| 100 | 2,702 / 2,402 | 781,251 / 643,326 | 2,577.58 / 2,416.68 | 2,720.12 / 2,527.27 |

At 100 schedules, statements fell 11%, returned bytes 18%, and median admission
time 6%. The ten-schedule p95 regressed. These small samples do not establish a
general latency guarantee. Admission still processes schedules serially and
performs eligibility and persistence work per job. The batch size of 100 limits
one pass, not the total number of registered schedules. There is no measured
2,000-job throughput claim or worker-capacity scaling claim.

## History and scoped reads

Forty samples per operation, with 100 schedules and 10,000 retained run records.
The workload lookup requests one deployment. The baseline ignores that filter and
returns the workspace's schedules; the new implementation filters in SQL.

| Operation | Statements before / after | Returned bytes before / after | Median ms before / after | p95 ms before / after |
| --- | --- | --- | --- | --- |
| Idle due scan | 2 / 2 | 0 / 0 | 0.57 / 0.77 | 2.59 / 2.07 |
| Workload schedule | 3 / 3 | 21,727 / 610 | 6.41 / 3.96 | 11.03 / 7.36 |
| Ten history rows | 2 / 2 | 1,529 / 1,529 | 4.93 / 3.99 | 7.69 / 6.64 |

Retained history does not contribute rows to the recurring due scan. The scoped
workload lookup returns 97% fewer bytes.

On the deployed local stack, PostgreSQL `pg_stat_statements` showed 49 due scans
over 49.11 idle seconds and 70 over 70.12 seconds with two active schedules.
Both scheduler replicas together issued approximately one due scan per second.
The active interval returned two due rows. Counters were sampled without resetting
statistics, filtering the cron due SELECT by its `next_run_at` predicate. This
verifies the local deployed polling rate, not a production query-insights claim.

## Live execution and dashboard

The existing local Compose stack ran two scheduler replicas and one customer
agent. The SDK scenario deployed two unique scheduled functions, observed four
successful executions of each, decoded their results, and deleted its app through
the public API. The cold function used four distinct containers; the warm function
reused one. Both revisions used the same local compute and cached Python image.

Run the exact module `python -m tests.e2e.local.function.scenario_schedule --live`
through `uv run --env-file .env --group workspace`, with a local authenticated
profile, workspace and `LAZYCLOUD_E2E_MACHINE` configured.

| Observation | Before, range ms | After, range ms | Samples per revision |
| --- | --- | --- | --- |
| Cold task creation after minute boundary | 212 to 864 | 40 to 82 | 4 |
| Warm task creation after minute boundary | 358 to 974 | 3 to 4 | 4 |
| Cold execution start after minute boundary | 1,177 to 1,646 | 852 to 1,176 | 4 |
| Warm execution start after minute boundary, excluding initial startup | 1,021 to 1,814 | 45 to 74 | 3 |

These timestamps illustrate removal of polling drift. Task creation timestamps
precede transaction commit, so they are not a measurement of durable admission.
Execution start and completion depend on available compute and are not scheduling
throughput metrics. This small live sample establishes execution and cleanup,
not a startup latency guarantee.

The exact Chromium test `scheduled workload renders its scoped live schedule`
passed against the deployed frontend and API. It verified the deployment-filtered
request, returned schedule, cron expression and UTC rendering. Its unique app was
paused during the browser check and deleted afterward.

## Correctness and delivery

Focused PostgreSQL/Redis tests cover concurrent admission of one occurrence,
rollback before commit, recovery after commit, transient admission failure,
replacement/removal/stop/pause/delete races, resume without replaying paused time,
authorized workload filtering and public run history. Existing function capacity,
claim, retry, lifecycle and migration checks passed. CI passed Python owner tests,
types, lint/format, client wheel installation and the web checkpoint.

Migration `0035_cron_occurrences` was applied to the local stack. All eight existing
run records survived with null occurrence fields. Deploy the control plane and
scheduler together, stopping old cron writers before new writers begin. The atomic
admission guarantee does not cover a mixed fleet containing the old scheduler.

The rewrite removes the per-function cron lock and duplicate lifecycle handling,
and uses shared function persistence. Across changed production Python files it
adds 465 lines and removes 339, a net increase of 126 including the 38-line
migration. It does not meet the code-reduction objective; the added occurrence
contract and transaction coordination buy durability. No new queue, lease service
or alternate execution implementation was introduced.

Account eligibility, concurrent-container quota checks for cold functions and
configured pending-task limits still apply. Physical worker availability does not
gate cron task creation. Exhausting an admission policy limit records a rejected
occurrence under the existing policy; accepted tasks wait for execution capacity.
