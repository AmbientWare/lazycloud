# Gateway package

Gateway control, backend dialing and prewarming, view projection, private-unit
gateway state, and request-event middleware, all behind explicit protocols.

Broad HTTP routing and process wiring stay in apps. RPC contracts live in
`shared.http.gateway`. Failures are typed errors rather than soft envelopes,
though a per-event status inside a stream is legitimate domain data.

Workspace isolation, authorization, reconnect behavior, framing, and route
cleanup are the invariants that matter here. A stream is a long-lived
authorization decision, not a single one made at connect time, and a route that
outlives its backend is a route that sends traffic nowhere.

Enrollment verifies provider identity before opening its write transaction.
The launch claim, join credential, machine, worker, enrollment, and provider
binding commit together through one supplied database session. Transactional
methods must not open another session or publish Redis state. Publish only
after commit, and recover retries from durable authority rather than issuing
another credential because a cache write failed.

A machine join command names the machine and the workspaces it serves. Minting
it writes the machine row, its workspace links, and a unit labelled by the name,
then issues one credential bound to that machine id. Reissuing for a pending
name revokes the earlier credential; reissuing for a name that is already
joined is a conflict until the host leaves. `lazycloud` and `aws` are refused as
names because they are the labels the platform derives for its own and
connected capacity. Leaving marks the machine deleted, which frees the name, and
removes the unit once nothing else holds it.
