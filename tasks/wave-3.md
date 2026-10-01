# Wave 3

Packets follow tasks/packet-brief.md and deliver their parity sections from
tasks/parity.md one to one.

| Packet | Parity sections | Migration | Proto field range |
| --- | --- | --- | --- |
| compute | Compute (managed, AWS connect, joined machines); agent install, enrollment, updates, interruptions | 0008 | 70-79 |
| billing | Billing and plans; usage metering; plan limits behind the identity, storage and execution seams | 0009 | none |
| observability | Logs, events and metrics (metrics, latency, account metrics, live change stream, task trace and lifecycle) | 0010 | 80-89 |
| web | Dashboard, plus every dashboard item listed in the other sections | none | none |
| workloads | Pods and devboxes; Sandboxes; Shells and SSH (after endpoints and storage merge) | 0011 | 90-99 |
| operations | Operations and administration (lazycloud-admin, admin settings), deployment automation, release CI | 0012 | none |

The web packet builds against the merged APIs and adds pages as the other
packets land. A packet whose API the dashboard needs lists the exact
operations in its report.
