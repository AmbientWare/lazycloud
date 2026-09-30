# Tests

- Apply the root test decision gate. Owner tests live beside their code; root
  tests prove material cross-owner behavior.
- Keep Python client and runner checks independent of backend imports. Shared
  wire examples belong in contracts and may be consumed by other languages.
- Isolate credentials, configuration, mutable globals and temporary resources.
  Tests must not inherit another checkout's environment or contact real accounts.
- Use real PostgreSQL and Redis for transaction, concurrency and recovery
  acceptance. Give each run its own database or namespace and clean it up.
- Use bounded independent observations for live acceptance. Assert refusal leaves
  the target unchanged and cleanup removes acquired resources.
