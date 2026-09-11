# Tunnel gateway app

This process owns the production lifetime of the WireGuard gateway. Peer
allocation and WireGuard configuration decisions live in `packages/networking`;
this app leases one indexed gateway identity, reads durable peers, reconciles the
Linux interface, and records observed handshakes.

Each identity owns a key and a public UDP Service. Different identities serve
concurrently; processes sharing an identity require the same Redis lease. Lease
loss removes the interface and health listener before another process can serve.
PostgreSQL owns the indexed gateway registry. Encrypted health probes prove each
client's path and distinguish accepting new connections from draining.

Shutdown publishes draining while keeping the interface and conntrack state.
Clients send new connections through other ready gateways. The process polls
established overlay connections for at most 120 seconds, within the pod's
150-second grace. The API's tunnel is a native Kubernetes sidecar so API request
draining finishes before its network disappears.

Preserve deployed gateway keys and Service objects. The ordered migration in
`deploy/active-gateways.md` prepares API contracts before publishing new agents
and keeps gateway zero serving while clients establish gateway one. Removing an
old endpoint requires migration of every authorized enrollment, including offline
agents that otherwise cannot reach their updater.
