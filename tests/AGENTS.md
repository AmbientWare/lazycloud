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
- A refusal needs a valid target and evidence that the refused action left it
  intact. A cleanup claim needs an acquired resource and evidence that it is
  gone. Expected values must come from the contract, not the function being
  tested or a copy of its internal plan.
- Use the smallest data set that distinguishes correct behavior from the
  relevant failure. Five rows can prove pagination and timestamp ties; a large
  data set needs a separate capacity or query-cost reason.
- Reserve broad matrices for distinct authorization, irreversible data-loss,
  paid-resource leak, concurrency, fencing, or destructive-cleanup transitions.
- Fakes stay typed and keep real error semantics: clients raise `HttpApiError`,
  services raise `shared.errors`. A fake that fails more politely than the real
  thing hides the handling that actually matters.
- Ordinary pytest tests require no external credentials. Live scenarios run
  only through an exact named module or browser scenario under `tests/e2e/`.

## Resource ownership

- Live acceptance can use complimentary billing for a designated test account.
  With an administrator profile, run `uv run --group workspace lazycloud-admin
  user set-complimentary <user-id> --grant`. This permits usage without a card,
  subscription, or credit balance; usage remains metered and Team-plan limits
  still apply. Select a workspace owned by that user when running the scenario.
  Do not treat missing billing setup as a blocker before checking this supported
  path. Preserve existing grants; revoke with `--revoke` only when removing a
  temporary grant created for the current acceptance run.
- `tests/database_fixtures.py` owns PostgreSQL databases, templates, pools, and
  transactions. Templates contain the migrated schema, then the default account
  and workspace with storage, then immutable billing history. Each is prepared
  once per pytest session. Only migration-owner tests run schema bootstrap
  themselves.
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
  runs one production app per session. Account-scoped cases create their own
  users and credentials. `api_workspace` creates a unique account and workspace
  per case; `api_client` authenticates that workspace's owner, selects its
  workspace, and clears client state afterward. Shared cases must scope every
  write and query to their workspace, avoid global enumeration, and leave app settings,
  dependency overrides, and background workers alone. Their data survives until
  session cleanup drops the runtime's database and Redis namespace.
- Use `isolated_services` when a scenario changes app configuration, tests
  global state, or owns startup and shutdown. `unpriced_services` uses the same
  composition for scenarios that publish their own rate history. These fixtures
  own their clients and pools; no caller closes them early.
- Async service scenarios use `async_services`. It shares the same composition
  and closes async clients in the event loop that used them, even when an
  assertion fails. Do not add local fixtures that close another fixture's clients.
- `tests/redis_fixtures.py` owns Redis lifetimes. `real_redis_actors` supplies a
  unique namespace, `async_redis` closes its client in the requesting event loop,
  and `stream_broker` stops its reader before the client closes. Cleanup removes
  only that namespace and checks for residue.
- Import scenarios opt into `isolated_imports`. It restores `sys.path` and
  unloads modules loaded from the case's `tmp_path`, restoring preexisting
  modules when a temporary import shadowed them.
- Use `tests.http_server.running_http_server` for local stdlib HTTP servers.
  It owns the serving thread, socket, and shutdown. Keep handlers in their test
  owner and preserve real timeout behavior when it is under test.
- Shared cleanup belongs in the fixture that acquired the resource. Do not add
  local copies of database creation, app construction, or server shutdown.
- Tests that construct an app own its client with a local context manager or
  `ExitStack`. Close the client and app lifespan before service fixtures tear
  down. A reusable app needs a documented state boundary; shared mutable globals
  are not test isolation.
- Advance an injected clock for elapsed-time decisions. Keep real delays only
  when the scenario proves transport timeouts, lease renewal, or another timing
  boundary. Bound polling and pagination loops so regressions fail with evidence.
- Delete compatibility, wrapper-only, presentation-literal, snapshot,
  repository-policy, inventory, and implementation-shape tests when you encounter
  them. Preserve migration tests that prove deployed data survives schema changes.
  Accessibility and layout tests stay only for a material user outcome nothing
  cheaper proves.
