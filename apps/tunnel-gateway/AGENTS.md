# Tunnel gateway app

This process owns the production lifetime of the WireGuard gateway. Peer
allocation and WireGuard configuration decisions live in `packages/networking`;
this app takes the active lease, reads durable peers, reconciles the Linux
interface, and records observed handshakes.

Only the lease holder may report Ready or configure the interface. A process
that loses the lease removes its interface before another replica can serve.
