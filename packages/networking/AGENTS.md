# Networking package

Authenticated agent tunnels, backend route dialing, and stream forwarding.
Gateway process composition stays outside.

Environment settings may satisfy runtime options directly where the semantics
match; add a conversion only to narrow secret or lifecycle scope or to produce a
transport contract. Origin validation, credential scope, reconnect, and cleanup
are this package's invariants.

- Dial the name as written and treat resolution as a recovery step, not a first
  step. Resolving on every request costs real work per request, and repeated
  lookups can disturb the very session the traffic depends on.
- Confine redialing to connect-time failures, which are raised before the request
  body is read, so recovery never has to replay a stream it already consumed.
- A dial that hangs against a correct address is usually not an addressing bug.
  Prove where the listener actually is before changing how a destination is
  addressed. The symptoms of a misplaced listener and of a stale name are
  identical from here.

A certificate admits a session; the enrollment authorizes what the session
carries. A stream runs until it ends, past its certificate's expiry and past a
renewal that replaced its session, because SSH and editor sessions last longer
than an hour. The gateway re-checks the enrollment every heartbeat and ends the
streams when that fails, so revocation still reaches them. A connected session
that reaches expiry without renewing is closed. All sessions of one agent share
one enrollment check per heartbeat, so a retired session holding streams adds
no queries.
