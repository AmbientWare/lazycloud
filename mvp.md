# MVP

What is left before this is a strong MVP, and what is deliberately not.

The product shape these are judged against: multi-tenant SaaS, where a customer
either joins their own machine (`lazycloud machine join`) or links their AWS
account and we provision into it. The fleet is largely hardware we do not own and
cannot fix. That is what makes the items below the ones that matter — most of
what a self-hosted engine needs (operator CLI, fleet management, breadth of
compute providers) is answering a problem this product does not have.

## Build

- [x] **Readiness gates routing; nothing reaps.** Shipped, and the reaping half is
  deliberately not built. Recorded at length because the reasoning is the
  decision, and the obvious next contributor will otherwise rebuild what was
  removed.

  What ships: a container is asked whether it is serving before it receives
  traffic. Pods dial the exposed port, or call an HTTP path when the Pod declares
  `health_check_path`; endpoints and ASGI call the `/health` the runner serves.
  The value is cold start and scale-up — before this, the first request to a
  booting container hit a port nothing had bound and errored. Verified against
  the local stack: a pod sleeping twenty seconds before binding holds the request
  and then answers, and a declared path returning 404 withholds traffic entirely
  rather than routing to a workload that is listening but not ready.

  What does not ship, and why. Reaping an unresponsive container was built and
  then removed. Three reasons, each sufficient:

  - *The structure already reclaims almost everything.* A container that exits, a
    worker that dies, a machine that vanishes, a start that never completes, a
    stranded row — all are handled by the scheduler-state TTL, the start
    deadline, and orphan reconciliation. A container that is merely idle is
    retired by its keep-warm window and replaced on demand. What remains is only
    a container wedged while still looking busy.
  - *The probe cannot see that case where it matters most.* Endpoints and ASGI
    are probed at `/health`, which the runner answers, so a hung handler with
    requests piled up in flight replies `200` while serving nothing. The one
    signal that observes user code is a declared health path, which today only
    pods can express. So the need and the signal overlap in exactly one place: a
    pod, with a declared path, wedged while holding connections.
  - *The mechanism is easy to get dangerously wrong.* The attempt bypassed the
    keep-warm and in-flight guards every other stop respects, could kill a
    sandbox a user was attached to, released a poison invocation to wedge each
    replacement without advancing its attempt count, and — writing the stop
    reason where the container is stopped — would have corrupted preemption
    settlement on a path that runs today.

  Gating without reaping is also where comparable platforms stop: a failed check
  drops the container from that round and it is probed again on the next one.
  Letting a declared path express readiness goes further than that, and is worth
  having on its own.

  If this is revisited, the next honest step is to surface a failing container
  rather than reap it — the detection is the hard half, and enforcement should be
  added to a signal already proven in production rather than trusted for the
  first time while it deletes containers. Automatic recovery beyond that needs a
  signal that does not depend on traffic arriving, which means the worker
  probing locally and publishing readiness on the container state both routing
  and scheduling already read.

- [x] **Stamp `private_worker` at worker registration.** Registration overwrites
  `workspace_id`, `owner_user_id` and `billing_owner` from the authenticated
  principal and does not overwrite this one. A customer holding root on their own
  joined machine can set `pool_mode: public` in the worker config; the record
  lands with `private_worker=False`, `can_fit` then treats it as shared fleet, and
  naming the pool `lazycloud` — every workspace's default — makes it a candidate
  for other accounts' workloads. Confidentiality holds, because the request stream
  refuses to deliver the container, but it is dispatched to that worker's queue
  first and the victim's container stalls until the start-deadline reclaim. A
  cross-tenant denial of service reaching the default pool.
  Done: stamped from the principal beside the other three, with
  `test_a_joined_machine_cannot_register_itself_into_the_shared_fleet` proving a
  joined machine that registers itself public is stored private.

- [ ] **Tell the owner their machine went away.** Recovery works and is silent.
  No event is emitted, the durable enrollment row and machine status stay `Ready`
  and `Running` indefinitely because they are only written when a heartbeat
  arrives, and nothing connects the container that moved to the machine that lost
  it. The live view does recompute — a machine list shows Offline with "Agent
  heartbeat is stale" about 60s in, and the dashboard renders it — so this is
  pull-only rather than absent. `plan_agent_disconnect` is the piece that would
  emit the event, write the disconnect, and disable the machine's worker; it is
  declared, unit-tested, and has no production caller.
  *Done when:* a machine going silent produces a workspace-scoped event its owner
  can see without asking us.

- [x] **A pod hitting its TTL names the platform as the reason.** The TTL stop
  passes no reason, so it takes the `User` default, which is in the set that
  settles claims as cancellations. A pod reaching its TTL therefore reports to
  every caller that their work was cancelled and charges the attempt, when a TTL
  is a platform-owned stop that should release. Done — though narrower than it
  reads: `stop` records no reason on the row and a pod holds no task claims
  today, so nothing observable changes yet. It states the rule where the next
  reader will find it rather than fixing a live symptom.

- [x] **Hot-path metrics.** Recording one wrote a database row, so nothing
  frequent could be instrumented and the readiness probe shipped without any.
  Done: `MetricsService` records through an OpenTelemetry meter, aggregates in
  the process, and pushes over OTLP on an interval. The durable path is gone —
  table, repository, records, the admin `/metrics` route nothing scraped, and
  `usage_to_prometheus`, whose only caller was that route. The scheduler now
  bootstraps telemetry for itself, without which the `autoscaler_*` and
  `worker_pool_*` metrics — most of them — would have gone quietly nowhere.
  Verified end to end against a collector in the local stack, on a metric the
  scheduler records rather than only one the API does.

## Verify

- [x] **A self-hosted machine only ever runs its own account's workloads.**
  Verified: it holds, and it fails closed. The load-bearing line is
  `scheduler/tools.py` `WorkerCapacity.can_fit`, whose first statement refuses a
  worker that does not serve the request's owner; `worker_serves_owner` in
  `shared/scheduling.py` returns False for an empty owner on either side. The
  owner is never caller-supplied — `SchedulerWorkerRequest` carries no owner
  field at all, it is resolved per dispatch from the workspace and overwritten at
  registration — and the same predicate is enforced again on the worker-repository
  side at handoff. `test_a_private_worker_refuses_another_accounts_request` covers
  it, and its docstring names the bug class: every workspace's default pool shares
  a name, so matching on the pool label alone would put one tenant's request on
  another tenant's machine.

- [x] **A workload survives its machine disappearing.** Verified: the work
  recovers, the machine's status does not. The machine leaves the schedulable set
  about 60s after its last telemetry, when the agent-pool controller disables its
  worker; its containers are reclaimed about 120–150s in, when the scheduler
  container-state TTL lapses and the autoscaler stops and replaces them. An
  in-flight request is handled well — a dial that fails before any byte is written
  re-selects a different container invisibly, and anything later is a bounded 502,
  503 or 504 rather than a hang. Billing stops with the machine, since metering is
  pushed from the worker.

  What does not close is the telling. See the visibility item below.

## Small

- [ ] **Every Blocked/Offline machine message names a cause the user can act on.**
  Customers cannot read our control plane, and when the fault is their own
  hardware we can only explain it. Cheap, and disproportionately what determines
  support load.

- [ ] **A skipped test says so.** Three env vars gate skips —
  `LAZYCLOUD_TEST_REDIS_URL`, `LAZYCLOUD_TEST_DATABASE_URL` and
  `LAZYCLOUD_TEST_POSTGRES_URL` — and all three skip silently, so a run reads as
  green while proving less than it looks like it proves. This has hidden a
  broken assertion twice. Two of the three name the same PostgreSQL instance and
  should not both exist.


## Deferred, deliberately

Recorded so they are not rediscovered as gaps.

- [ ] **Durable block disks with snapshots.** A primitive this platform lacks: a
  sized block device with a filesystem and a restore point, which is what
  anything stateful wants and a shared volume mount is not. Under
  bring-your-own capacity the disk is on the customer's infrastructure and often
  already provisioned, so it does not block this MVP.
- **A reachable `/health` on function endpoints** was considered and refused. The
  runner serves the route; the endpoint router declares no subpath, so nothing
  can call it. Reaching it means accepting arbitrary subpaths on a surface whose
  contract is `POST /` with a payload — changing what a deployed endpoint answers
  in order to expose a probe. ASGI routes a subpath already and does not have the
  problem. `apps/api/src/api/server/routers/endpoints.py` records the limit where
  a reader meets it.
- Compute provider breadth, fleet operator CLI, and packaged workload types
  (LLM serving, databases, MCP hosting) are **not** planned. Machine join plus the
  AWS connection already covers the compute story for this product.
