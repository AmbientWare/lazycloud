# Tunnel gateway app

This process owns the production lifetime of the WireGuard gateway. Peer
allocation and WireGuard configuration decisions live in `packages/networking`;
this app takes the active lease, reads durable peers, reconciles the Linux
interface, and records observed handshakes.

Only the lease holder configures the interface and exposes the TCP target-health
listener. Kubernetes keeps both running replicas ready so the NLB can select the
active target. Platform peers use the same public endpoint as agents because the
ClusterIP includes the standby and cannot apply the NLB's active-target health
check. A process that loses the lease removes its interface and closes its listener
before another replica can serve.
