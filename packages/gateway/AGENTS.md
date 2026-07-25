# Gateway Package

Own gateway control, backend dialing/prewarming, view projection, private-pool
gateway state, and request-event middleware behind explicit protocols. Broad
FastAPI routing and process wiring remain in apps. RPC contracts live in
`shared.http.gateway`; typed errors replace soft envelopes, while deliberate
per-event streaming statuses remain valid. Preserve workspace isolation,
authorization, reconnect, framing, and route cleanup through the real stream.
