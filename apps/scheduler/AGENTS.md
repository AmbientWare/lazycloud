# Scheduler app

Scheduler settings, concrete adapter composition, and the process loops.

Scheduling decisions stay in `packages/scheduler`; this app chooses the concrete
adapters and runs the loops. Environment names here are part of the deployment
contract and change together with the deployment assets that set them.

## Four loops, one process

`Scheduler` does the work and `loops.py` decides when. The passes exist because
their cadences differ, not because their work is unrelated:

- **placement**, every 1s, decides what needs to run. Autoscalers and
  function retries, and nothing that calls a service outside the cluster.
- **capacity**, every 5s, keeps the fleet and its records agreeing. Also billing
  enforcement, which touches only Postgres and whose interval is money, and cron
  firing, because a schedule that fires late was wrong.
- **housekeeping**, every 30s, is everything that waits on somebody else:
  Stripe, S3, Cloudflare.
- **dispatch** places what is already ready, woken by Redis rather than a clock.

They were one loop, and a caller waiting for a container waited for whatever
that loop was doing. A tick that drained the meter outbox to Stripe took twenty
to thirty-five seconds, and placement ran eleventh in it, so a function task
took fifty-five seconds to start and a scheduled one averaged thirty.

What makes the separation safe is that nothing in a tick depended on its order.
No pass consumes another's result, and the guarantee that an unpaid account
cannot start work is not one of these passes: admission reads three Postgres
rows in the transaction that reserves the container, and the allowance it reads
is written when usage is priced.

Keep a reconciliation in exactly one loop. The interval stamps on `Scheduler`
are read-then-write with no lock, which is correct while each has one caller and
races the moment it has two. That, not the work itself, is the constraint.

`run_once` runs every pass in sequence and the running process never calls it.
It is what `--once` means, and what a test uses to ask for a whole sweep without
waiting for four cadences to coincide.

## Liveness is per loop

Each loop stamps its own heartbeat file, `<base>.<loop>`, and the probe reads
all of them. One file for the process answers the wrong question: the process
outlives a loop that has stopped beating, which is the failure worth catching.

The beat lands after a pass rather than before it. Before, it says a pass was
attempted, and stays fresh while the work under it is wedged.

The threshold is set by the slowest loop. Housekeeping waits on Stripe and S3,
so a threshold near its cadence restarts a scheduler for being slow rather than
for being stuck. Changing a cadence means revisiting `livenessMaxAgeSeconds` in
the chart, the Compose healthcheck, and the scheduler image healthcheck. All
invoke `scheduler_app.health`, which owns loop names and heartbeat paths shared
with the running process.

## Stopping

SIGTERM sets one event and every loop stops on it. Without that the process died
where it stood on the signal Kubernetes sends: no telemetry flush, no thread
join, no lease released. Handlers install only on the main thread, so the main
thread waits on the event rather than on the loops.
