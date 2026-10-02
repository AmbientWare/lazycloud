# Wave 3

Packets follow tasks/packet-brief.md and deliver their parity sections from
tasks/parity.md one to one.

| Packet | Parity sections | Schema | Proto field range |
| --- | --- | --- | --- |
| compute | Compute (managed, AWS connect, joined machines); agent install, enrollment, updates, interruptions | Compute | 70-79 |
| billing | Billing and plans; usage metering; plan limits behind the identity, storage and execution seams | Billing | none |
| observability | Logs, events and metrics (metrics, latency, account metrics, live change stream, task trace and lifecycle) | Observability | 80-89 |
| web | Dashboard, plus every dashboard item listed in the other sections | none | none |
| workloads | Pods and devboxes; Sandboxes; Shells and SSH (after endpoints and storage merge) | Execution | 90-99 |
| operations | Operations and administration (lazycloud-admin, admin settings), deployment automation, release CI | none | none |

The web packet builds against the merged APIs and adds pages as the other
packets land. A packet whose API the dashboard needs lists the exact
operations in its report.

## Workload API unification (after workloads and web merge)

One `WorkloadSpec` with a `kind` and kind-specific sections replaces
`FunctionSpec`. One resource path, `/apps/{app}/workloads/{kind}/{name}`,
serves describe, versions, stop, start, delete, scale, logs, containers and
performance for every kind, and replaces both the per-kind describe routes and
the separate `/deployments/{deployment}` scheme. Kind-specific routes stay only
where behavior differs: `/tasks` for functions, `/invoke/...` for HTTP kinds,
`/devbox/start|stop` and `/ssh`. The old routes are deleted in the same change,
with no aliases. The SDK, CLI and web move together, and the packet's net
handwritten code must go down.

## Final acceptance sweeps

- A duplicate and dead-code sweep over the merged Go, Python and web trees.
- A handwritten-code comparison against the reference for each capability,
  recorded in tasks/acceptance.md.
