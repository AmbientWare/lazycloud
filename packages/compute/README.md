# Fleet planning

Warm nodes serve container requests. Compatible stopped or hibernated nodes
replenish warm capacity; purchases replenish reserves. `FleetCapacityPolicy`
owns resource floors, ceilings, activation estimates and spending limits.

Run the production planning decisions without creating provider resources:

```bash
uv run python -m compute.simulation --scenario quiet
uv run python -m compute.simulation --scenario scheduled-burst
uv run python -m compute.simulation --scenario rollout-runtime-1000
uv run python -m compute.simulation --scenario rollout-pressure-failure
uv run python -m compute.simulation --input scenario.json
```

`simulation.Scenario` defines custom input. Supply node offers and prices,
workload arrival times, resources and durations, initial capacity, policy and
lifecycle timings. `known_at` marks a scheduled arrival. Output includes capacity
misses, estimated costs, packing, churn and startup delays. Rollout scenarios
include temporary cost and capacity retained through failure cleanup.

The bundled prices and timings are synthetic. Simulation excludes network,
storage, tenant isolation and real application readiness. It cannot establish
production latency. The scheduled burst still exceeds the default warm ceiling;
the report exposes the resulting misses rather than ignoring the budget.

Inspect observed function container readiness separately:

```bash
uv run lazycloud-admin scheduler startup-latency --workspace-id WORKSPACE_ID
```

The report includes failed and overdue starts, missing readiness evidence and
phase percentiles. Container readiness is not user-code execution entry. Warm
execution remains unmeasured, and the function runner's 100 ms empty-claim poll
can consume the warm-start target by itself.

Forecast activation horizons are explicit estimates. Existing provider records
do not preserve each resume-to-fresh-intake interval, so they cannot supply a
historical resume percentile. Request placement retains region, runtime, GPU,
storage and tenant constraints; fleet headroom forecasts aggregate by purchase
market and GPU type and do not guarantee spare capacity in every requested zone.
If another release arrives while an update's reserved replacement is busy, that
replacement finishes its current work before updating.

Migration `0028_capacity_maintenance` changes rollout ownership and requires the
coordinated controller cutover in [the deployment runbook](../../deploy/RUNBOOK.md).
An older controller build cannot run against that schema.
