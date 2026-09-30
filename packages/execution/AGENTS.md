# Execution

- Own execution-resource workflows and pure deterministic planners. Use explicit
  protocols; keep concrete scheduler, worker, gateway and provider owners outside.
- Resolve placement through `placement.workload_placement` before reserving a
  container. Deployments retain their pin; other requests resolve current workspace
  and named-machine placement, failing explicitly when unavailable.
- Functions are the task execution model. The durable task's container claim is
  the execution fence; only live containers may claim. Mark a container terminal
  before settling its claims, and recover abandoned claims.
- Platform stops release work without spending retry attempts; self-caused failure
  consumes attempts. Stop reasons also affect worker exit interpretation and public
  results. Shared-container drains use `Scheduler`, not cancelling user reasons.
- Idle retirement closes admission under capacity and claim fences. Keep physical
  allocation and billing until exit is observed; preserve attempt completion times.
- Cancellation kills only the selected process slot and rejects stale monitor
  responses. Shared-interpreter cancellation stops the container and releases
  unselected work with its attempt budget intact.
- Cron is a function property, not a workload kind. Reject cron on other kinds.
  Reconcile one schedule per resource on every deploy, including removal; stopped
  or deleted deployments cancel their scheduled invocations.
