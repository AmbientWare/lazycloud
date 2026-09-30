# Connection gateway

- Own listener and outbound agent tunnel lifetime; keep domain behavior in packages.
- Local keys require owner-only permissions. API credentials obtain leaf
  certificates, never issuer keys. Install credentials atomically without
  interrupting established streams.
- Readiness covers the listener, certificate, PostgreSQL and Redis, with freshness.
- SIGTERM removes readiness, requests agent reconnect and allows the 60-second
  stream drain. Join maintenance before closing clients; clean up on startup failure.
