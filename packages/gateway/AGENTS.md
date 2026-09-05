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
