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
