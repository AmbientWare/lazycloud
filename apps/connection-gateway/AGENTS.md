# Connection gateway app

This process owns listener and dependency lifetime for outbound agent tunnels.
Transport, authentication, session ownership, and route authorization stay in
their domain packages. API and scheduler releases do not restart this process.

Generate the gateway key locally and protect it with owner-only permissions.
Bootstrap and renew its leaf through the API's dedicated gateway credential.
The gateway receives its certificate and public trust bundle, never the issuer
key. Publish certificate and trust changes through `TunnelCredentials.install`;
new handshakes read one complete generation while accepted streams continue.

The advertised pod address is distinct from the listening address. Readiness
requires a valid certificate, a started listener, PostgreSQL, and Redis. The
readiness file is refreshed after healthy probes; deployment probes must check
its age, so a stalled process cannot remain ready indefinitely.

SIGTERM removes readiness and tells agents to reconnect before the listener
drains. Accepted streams receive up to 60 seconds, after which gRPC cancels them.
Finish gateway maintenance before closing Redis and PostgreSQL. Cleanup must
attempt each owned resource even when another cleanup step fails.
