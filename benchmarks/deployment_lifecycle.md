Local measurements for the apps and deployments rewrite, September 30, 2026.

Run `uv run --group dev pytest -s -q benchmarks/deployment_lifecycle.py` with
`compose.test.yaml` running. The benchmark creates and removes isolated PostgreSQL
databases and Redis namespaces through the existing fixtures. It uses production
services without a worker or provider substitute.

The baseline is commit `48176c28c`, run in a separate worktree and virtual
environment. Copy the benchmark there, omit the new `idle_deployments` measurement,
and use `management.set_deployment_active` for the baseline stop/start operation.
Both runs used the same PostgreSQL 18 and Redis 8 test containers, sequentially,
with one idle local Compose stack and no concurrent image builds. Each read warms
once, then takes 40 samples at 1, 10 and 100 retained versions. The table shows
the 100-version case. Bytes count returned PostgreSQL field payloads, excluding
protocol framing. Statements include transaction-local settings.

| Operation | Median ms, before → after | p95 ms, before → after | SQL statements | Returned bytes |
| --- | --- | --- | --- | --- |
| Resolve deployment name | 20.73 → 1.77 | 124.22 → 3.24 | 3 → 3 | 338,202 → 3,487 |
| List latest deployments | 20.59 → 2.17 | 128.09 → 2.35 | 3 → 3 | 338,202 → 3,487 |
| App summaries | 23.40 → 3.82 | 129.50 → 6.67 | 11 → 7 | 339,197 → 3,490 |
| Check invocation state | 1.46 → 0.63 | 1.59 → 1.19 | 3 → 2 | 3,741 → 19 |
| Idle app reconciliation | 0.66 → 0.71 | 0.70 → 1.08 | 2 → 2 | 0 → 0 |
| Release admission, no workers | 0.134 → 0.022 | 0.158 → 0.034 | 0 → 0 | 0 → 0 |
| Prepare and publish | 50.44 → 35.14 | 70.51 → 45.07 | 101.5 → 61 | 50,149 → 40,506 |
| Stop and start | 18.94 → 19.70 | 22.29 → 22.92 | 39 → 39 | 15,998 → 16,642 |

Prepare and publish calls both gateway service operations. It publishes another
workload 41 times, including warmup, after creating the retained history. Stop/start
also takes 40 samples after warmup, with no running containers. Write-operation
statement and byte counts are means across the measured samples. Stop/start now
persists recovery work; this measurement shows a small latency and byte increase.

The new idle deployment recovery pass takes 0.97 ms median and 1.72 ms p95, with
four statements and zero returned bytes. Its two reads use due-work indexes.
With two fleet-controller replicas, the shared 30-second housekeeping cadence
budgets eight statements per minute, including transaction settings. A 62.41-second
`pg_stat_statements` observation on the running local stack recorded two calls to
each read, zero rows, and 0.045 ms combined database execution time. Production
query insights have not been observed; no production deployment was performed.

An intermediate latest-version query still took about 22 ms. `EXPLAIN ANALYZE`
showed its version-selection subquery executing 100 times. Materializing the
selected IDs once removed that repeated work. The final query returns two rows
including workspace resolution, compared with 101 before.

The main focused acceptance run passed 97 tests. A subsequent 19-test run covered
workspace deletion, provider-preparation recovery, runtime configuration and the
benchmark after the final cleanup changes. Repository Ruff checks and changed-owner
type checking also passed.

The final Compose build passed the public SDK lifecycle scenario. It built and
invoked a function, redeployed it with both versions retained, stopped and started
the latest deployment, paused and resumed the app, invoked again, and deleted the
app. The returned values were 49 and 64. No deployment effects, preparations or
task-created live containers remained. Run it with `LAZYCLOUD_E2E_MACHINE=local-agent`
and the local endpoint, token and workspace configured:
`uv run --env-file .env --group workspace python -m tests.e2e.local.function.scenario_invoke --live`.

These timings measure owner service work, not network HTTP latency, container
startup or agent readiness. The live scenario establishes successful execution,
not a before/after startup claim. Connected AWS grant recovery was checked through
the real database and a recording provider boundary; live AWS IAM behavior was
not re-tested. The provider adapter is unchanged.

The complete change removes 85 production Python lines, including the new 90-line
migration. That net reduction is modest. The larger simplification is the removal
of the compensating deployment registrar and duplicate management orchestration,
with one publication transaction and durable, shared post-commit recovery.
