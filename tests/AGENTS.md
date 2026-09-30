# Tests

- Apply the root test decision gate. Package/app tests belong with their owner;
  root tests prove concrete cross-owner behavior.
- Assert material outcomes with the smallest dataset. Use typed fakes only at
  genuine boundaries; preserve real error semantics. Ordinary tests need no
  external credentials.
- Use shared PostgreSQL fixtures. Savepoint-backed `service_context` is for
  synchronous work; concurrency, async and real commits require committed contexts
  or seeded database URLs. Schema setup belongs to migration fixtures.
- Reuse account/workspace builders. Isolate lifecycle/global service changes;
  do not mutate shared API runtime state. Async services own their cleanup.
- Redis fixtures own namespace and event-loop client cleanup. Reuse HTTP fixtures
  and injected clocks unless real timing is the behavior under test.
- Refusal preserves the target unchanged; cleanup proves acquired resources gone.
  Poll bounded independent signals for acceptance rather than waiting silently.
- Never test E2E harnesses or import their internals. Live billing scenarios use
  approved test accounts and preserve existing grants, revoking only their own.
