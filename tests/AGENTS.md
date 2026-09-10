# Tests

Whether a test should exist at all is decided by the test decision gate in the
root `AGENTS.md`. This file covers where tests live and what they may assert.

- Owner tests live beside their package or app. `tests/integration` owns concrete
  cross-owner workflows, and architecture or deployment tests exercise real
  boundaries rather than scanning the repository. Only shared fixtures live at
  the root.
- The root autouse fixture stays limited to cheap environment and settings
  isolation. Pull in the full service graph only through the owner or integration
  `conftest.py` that actually needs it.
- Make production behavior work first. Assert the resulting state, response,
  error, side effect, resource residue, or cleanup. Do not assert construction,
  constants, types, signatures, calls, ordering, plans, commands, mocks, or
  source inventories.
- Prove each invariant once, at its cheapest authoritative owner. Duplicate a
  scenario higher up only for a boundary risk the lower test cannot see.
- Reserve broad matrices for distinct authorization, irreversible data-loss,
  paid-resource leak, concurrency, fencing, or destructive-cleanup transitions.
- Fakes stay typed and keep real error semantics: clients raise `HttpApiError`,
  services raise `shared.errors`. A fake that fails more politely than the real
  thing hides the handling that actually matters.
- Tests stay deterministic and require external credentials only when explicitly
  marked integration or smoke.

## Resource ownership

- `tests/database_fixtures.py` owns PostgreSQL databases, templates, pools, and
  transactions. Templates contain the migrated schema, then the default account
  and workspace with storage, then immutable billing history. Each is prepared
  once per pytest session. No ordinary test runs its own schema bootstrap.
- `service_context` is the default for synchronous owner services. It borrows a
  session-scoped pool and binds sessions to one connection with savepoints. The
  fixture rolls back the outer transaction after each test. Do not use it for
  independent connections, async database clients, or commit visibility.
- `committed_service_context` supplies the same seed in a separate database.
  Use it for concurrency, advisory locks, and effects that require real commits.
  `seeded_database_url` supports scenarios that configure their own pool or
  replicas. The test closes its clients; the fixture drops its database.
- `database` supplies a migrated database without domain seed. Migration tests
  use `postgres_database_url` for an empty database. Rate-publication scenarios
  can use `workspace_database` without the suite's billing history.
- `tests/workspaces.py` owns account, workspace, and credential data builders.
  Pass a domain context rather than an app graph. Construct the production
  service being exercised with its actual required dependencies.
- `apps/api/tests/runtime.py` owns API composition and lifespan. `api_runtime`
  runs one production app per session. `api_workspace` creates a unique account
  and workspace per case; `api_client` adds credentials and workspace selection
  and clears client state afterward. Shared cases must scope every write and
  query to their workspace, avoid global enumeration, and leave app settings,
  dependency overrides, and background workers alone. Their data survives until
  session cleanup drops the runtime's database and Redis namespace.
- Use `isolated_services` when a scenario changes app configuration, tests
  global state, or owns startup and shutdown. `unpriced_services` uses the same
  composition for scenarios that publish their own rate history. These fixtures
  own their clients and pools; no caller closes them early.
- `real_redis_actors` owns a unique namespace and its clients. Stop producers
  before its teardown. Cleanup removes only that namespace and checks for residue.
- Use `tests.http_server.running_http_server` for local stdlib HTTP servers.
  It owns the serving thread, socket, and shutdown. Keep handlers in their test
  owner and preserve real timeout behavior when it is under test.
- Shared cleanup belongs in the fixture that acquired the resource. Do not add
  local copies of database creation, app construction, or server shutdown.
- Delete compatibility, historical-migration, wrapper-only, presentation-literal,
  snapshot, repository-policy, inventory, and implementation-shape tests when you
  encounter them. Accessibility and layout tests stay only for a material user
  outcome nothing cheaper proves.
