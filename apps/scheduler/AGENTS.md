# Scheduler and fleet controller processes

`lazycloud-scheduler` owns execution placement, dispatch, cron and image builds.
`lazycloud-fleet-controller` owns acquisition, fleet reconciliation and external
maintenance. Both entrypoints ship in the scheduler image. They construct
separate service records; execution never constructs provider clients, billing
outboxes, volume maintenance or lifecycle mutation services.

The domain owners live in `packages/scheduler`. Apps compose adapters and own
process lifetime. Shared state crosses PostgreSQL and Redis, never process-local
objects. Deployment settings change with chart and Compose consumers.

Placement and dispatch wake on Redis signals. Placement coalesces wakes for at
least 100ms between passes. A separate recovery loop checks a Redis cadence claim
every 100ms; its winner performs the one-second timeout, retry, unpublished
request and autoscaling recovery sweep. The claim survives a completed pass
until expiry, suppressing duplicate SQL reads across replicas. If its holder
dies, another replica can claim after one second. Durable target claims and
request fencing remain authoritative when passes overlap.
Cron and build submission have independent one-second loops. Build recovery runs
every five seconds and artifact cleanup every thirty seconds.

Fleet acquisition wakes on recorded demand and recovers every five seconds.
Contended owners retry independently with bounded backoff. Capacity reconciliation
runs every five seconds; external billing, storage and domain maintenance every
thirty seconds. Billing enforcement remains in the capacity pass. Admission
checks the durable billing allowance in its transaction.

Reservation cancellation removes the allocation under its owner lease. Fleet
reconciliation releases an unallocated purchase or fulfills capacity that has
registered. Execution dispatch does not invoke provider cleanup.

Every loop owns one heartbeat stamped after a completed pass. Each process's
health command checks only the loops that process runs. SIGTERM stops and joins
its loops before closing database, Redis and storage clients. Per-loop failure
backoff cannot throttle another loop. An interval stamp has one caller.

`--once` invokes the selected process's passes once. It never composes the other
process or substitutes for deployed cross-process acceptance.
