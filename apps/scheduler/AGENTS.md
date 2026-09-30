# Scheduler processes

- Execution and fleet processes compose separate owners. Execution does not
  construct provider or billing-maintenance clients; fleet owns provider cleanup.
- Domain packages own decisions; apps own wakes, cadence, heartbeats and shutdown.
- Coordinate recurring cadence across replicas, including after a pass releases
  its lock, so idle replicas do not repeat the same database reads.
- Heartbeat only after a successful pass. Keep failure backoff independent and
  join loops before closing clients. `--once` runs only the selected process.
- Keep process settings synchronized with deployment consumers.
