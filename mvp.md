# MVP

What is left before this is a strong MVP, and what is deliberately not.

The product shape these are judged against: multi-tenant SaaS, where a customer
either joins their own machine (`lazycloud machine join`) or links their AWS
account and we provision into it. The fleet is largely hardware we do not own and
cannot fix. That is what makes the items below the ones that matter — most of
what a self-hosted engine needs (operator CLI, fleet management, breadth of
compute providers) is answering a problem this product does not have.

## Build

- [ ] **Recover a container that stops serving.** Routing now refuses a container
  that fails its readiness probe, and nothing ever acts on that: a wedged
  workload stays wedged, silently, until a person notices. Silence is worse than
  the old behaviour, which at least failed loudly. Decide the owner — the
  scheduler is the candidate, since it already owns preemption, reclaim, and
  orphan reconciliation, and is the only party that can also stop billing for a
  container that is alive but useless. Matters more here than on a curated fleet:
  a customer's desktop throttles, swaps, and shares its GPU.
  *Done when:* a container that stops answering its probe is reaped and replaced
  without a human, its billing stops, and the deployment's own containers are not
  disturbed.

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
