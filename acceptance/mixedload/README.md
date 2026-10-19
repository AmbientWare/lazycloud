# Mixed load

Tests the fleet against a live stack: a recorded mix of CPU and A10G
functions arrives on its recorded schedule, and `report.sql` reads how the
fleet served it from the stack's database. Use it before and after a change
to placement, the planner, reserves or launches, on a stack nothing else
loads.

Run the recording, or two bursts of 30 one-CPU tasks 90 seconds apart:

```sh
export LAZYCLOUD_ENDPOINT=https://api.lazycloud.dev LAZYCLOUD_TOKEN=... LAZYCLOUD_WORKSPACE=<workspace>
uv run python acceptance/mixedload/mixedload.py            # recording.json at its recorded pace
uv run python acceptance/mixedload/mixedload.py recording.json 2   # twice as fast
uv run python acceptance/mixedload/mixedload.py bursts
```

Each run deploys a new app, `mixedload_<MMDDHHMM>`, printed first. Then:

```sh
psql "$DATABASE_URL" -v app=mixedload_<MMDDHHMM> -f acceptance/mixedload/report.sql
```

The report gives, for the run's window:

- each function's wait to placement and to ready, p50 and p95;
- containers that did not finish cleanly;
- hosts the fleet requested: launched, out of capacity, pools tried, launch
  time, reserves;
- reserve wakes and their time to ready;
- Spot interruptions;
- fleet cost, and the part bought at a loss.

The recording's GPU functions are not preemptible, so they buy On-Demand
A10G hosts and test GPU reserves and packing, not the Spot fallback. Scheduler
logs carry each launch's placement score
(`"instance launched"`, `"launch moved to the next pool"`).
