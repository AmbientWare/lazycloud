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

  beta9 (AGPL-3.0) draws the same line and stops earlier: on a failed check it
  omits the container from the round and re-probes on the next tick
  (`pkg/abstractions/endpoint/buffer.go`, `pkg/abstractions/pod/proxy.go`). It has
  no user-declared health check at all; its deeper probes are platform-supplied
  per workload type. This platform is ahead on the declarative signal and level
  on gating.

  If this is revisited, the next honest step is to surface a failing container
  rather than reap it — the detection is the hard half, and enforcement should be
  added to a signal already proven in production rather than trusted for the
  first time while it deletes containers. Automatic recovery beyond that needs a
  signal that does not depend on traffic arriving, which means the worker
  probing locally and publishing readiness on the container state both routing
  and scheduling already read.

## Verify

Each of these is a question, not a known defect. Either may be zero work.

- [ ] **A self-hosted machine only ever runs its own account's workloads.**
  `capacity_owner_id` and the pool model suggest this holds; it has not been
  checked. A wrong answer here is an incident rather than a backlog item, which
  is why it is on the list ahead of things that are certainly missing.
  *Done when:* the enforcement point is named, and any path by which another
  tenant's container could be placed on joined hardware is either shown not to
  exist or fixed.

- [ ] **A workload survives its machine disappearing.** The signature failure of
  this product: someone closes the laptop their deployment is running on. The
  machinery exists — orphan reconciliation, reclaim, readiness phases through
  Joining/Ready/Blocked/Offline/Revoked. What is unverified is whether the
  user-visible story closes.
  *Done when:* the work lands on other capacity, and the owner is told why it
  moved, through a surface they can reach without us.

## Small

- [ ] **Every Blocked/Offline machine message names a cause the user can act on.**
  Customers cannot read our control plane, and when the fault is their own
  hardware we can only explain it. Cheap, and disproportionately what determines
  support load.

- [ ] **Give function endpoints a reachable `/health`.** The runner serves it; the
  routes declare no subpath, so nothing can call it. ASGI endpoints already work.

## Deferred, deliberately

Recorded so they are not rediscovered as gaps.

- [ ] **Hot-path metrics.** `observability.metrics` writes a database row per
  observation, so anything frequent cannot be instrumented — which is why the
  readiness probe ships without any. Not blocking, but it is the reason the
  restart work above will be debugged by reading logs instead of a graph.
- [ ] **Durable block disks with snapshots.** A real primitive we lack (beta9 has
  one). Under bring-your-own capacity the disk is on the customer's infrastructure
  and often already provisioned, so it does not block this MVP.
- Compute provider breadth, fleet operator CLI, and packaged workload types
  (LLM serving, databases, MCP hosting) are **not** planned. Machine join plus the
  AWS connection already covers the compute story for this product.
