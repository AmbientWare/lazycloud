# Networking Package

Own protocol-neutral route planning, backend dial targets, and tailnet helpers;
gateway composition remains outside. Environment settings may satisfy runtime
options directly when semantics match; add conversions only to narrow secret or
lifecycle scope or produce a transport contract. Preserve origin validation,
credential scope, reconnect, and cleanup through the real dial/tailnet boundary.

The tailnet name is the route and peer resolution is the recovery step. A dial
goes to the name as written; only when that fails does the netmap get consulted,
because resolving first costs a loopback hop and two `tailscale status`
executions per request, and three failed lookups inside a minute reach the
control-session refresh that runs `tailscale down` — ordinary traffic taking out
the node's own session. Redialing is confined to connect-time failures, which
httpx raises before it reads the request body, so recovery never has to replay a
stream it already consumed.

A dial that hangs against a correct address is not this layer's bug to fix. The
symptom of a control plane whose tailnet sidecar sits in a dead network
namespace is identical — correct name, correct peer address, connection times
out — and it has been misread here before as stale DNS. Prove where the
listener is before changing how a destination is addressed; `deploy/AGENTS.md`
carries the check.
