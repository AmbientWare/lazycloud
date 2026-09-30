# Repository rules

Edit `AGENTS.md`, never its `CLAUDE.md` symlink. Add that symlink with any new
guidance file. Keep permanent rules here; service rewrite goals and process
rules belong in root `update.md`.

## Ownership and code

- Domain packages own decisions and workflows; repositories map and query
  persistence; apps own composition and process lifetime. Handlers, commands,
  schedulers and workers call the responsible owner through explicit dependencies.
- Keep cross-service coordination separate from each service's domain decisions.
  Give shared behavior and configuration one owner; separate configuration only
  for distinct permissions, tenant isolation or lifetimes.
- `shared` owns backend-free contracts, `lazycloud` the public SDK/CLI, and
  `runner` code inside user containers. SDK and runner depend only on shared
  contracts and user code. Providers implement neutral domain protocols.
- PostgreSQL/SQLAlchemy/Alembic own durable state; Redis owns transient
  coordination; object stores and filesystems own bytes. Do not duplicate stores.
- Fix ownership, models, boundaries and signatures. Do not hide defects with
  `type: ignore`, broad `Any`/`object`, casts, checker exceptions, shims,
  compatibility wrappers or fallback imports.
- Use one production implementation. No fake success, weaker backends or switches
  that give tests another path. Missing capabilities fail with a named reason.
  Remove obsolete paths and update consumers together.
- Use Python 3.12 types, Pydantic v2 at boundaries, precise enums/dataclasses/
  protocols and selective exports. Preserve omitted values versus explicit zero.
  Read consumers before changing an enum, status, sentinel, default or contract.
- Work from the root with `uv`; use Bun for web packages, `apply_patch` for
  manual edits, and Ruff for Python formatting/imports.
- Comments explain only non-obvious current constraints. Load the `unslop` skill
  before writing prose. Describe LazyCloud directly; name external products only
  for actual dependencies, integrations or operational steps.

## Database and contracts

- Filter, aggregate and project in SQL. Lookups must not load collections.
  Recurring work scans live resources and due work, batches shared reads and
  reuses snapshots rather than scanning retained history.
- Keep locks only for concrete concurrency invariants; use the narrowest scope
  and measure contention. Budget recurring reads across replicas and cadence.
  For changed polling/reconciliation/lookups, record query counts and returned
  bytes before/after with representative history, idle and active; verify the
  deployed rate in query insights.
- Use `/api/v1/<resource>` for resources and `/gateway/*` for RPC. Authorize
  every requested workspace against token scope and membership; only an
  administrator may reach a tenant workspace without membership.
- FastAPI handlers validate, authorize, call one service and map typed results.
  JSON payloads use `HttpModel`, precise response models, stable operation IDs,
  `{data, next}` lists, `datetime` and `204` for bodiless success.
- Services raise `shared.errors`; central handlers map `ErrorResponse`.
  SDK/CLI transport errors use `HttpApiError`. Synchronize contracts, SDK/CLI,
  runner and web Zod consumers in the same change.
- `0001_relational_baseline` and every deployed migration are frozen.
  Add a chained Alembic revision; never infer permission for a production reset.

## Acceptance

- Define the user-visible outcome and cheapest authoritative evidence first.
  Validate a coherent owner or cross-owner change with focused checks; use the
  real service/container/provider when that boundary changes. Broad gates are
  for releases or explicit broad quality claims.
- Add or retain a test only if it proves a material production invariant at a
  stable owner/public boundary, provides unique evidence, and is cheaper and more
  maintainable than production-representative acceptance. Remove encountered
  tests that fail this gate.
- Protect authorization, data integrity, durability, concurrency, cleanup and
  public outcomes. Do not test implementation shape, mock call order, wiring,
  copy, snapshots, harnesses or behavior already proven elsewhere.
- Owner tests live beside their package/app; root tests cover cross-owner
  behavior. Opt-in E2E runs use an exact named module or browser node, never
  Python file paths. Do not import E2E internals into pytest.
- Run focused tests directly, prefer `pytest -x`, and keep output observable.
  Diagnose with bounded polling of durable state, logs and external signals;
  investigate stalled progress instead of silently waiting for a terminal state.
- Missing credentials/services are acceptance gaps, not permission for mocks.
  Fix failures caused by the change; name unrelated failures and unverified
  boundaries. Clean up every acceptance resource the task created.

## Safety and delivery

- Inspect the dirty tree, preserve unrelated work and stage intentional files.
  Never expose secrets in output, logs, URLs, tests, comments or durable records.
- Treat external systems as shared. Inspect before changing them; delete only
  exact resources proven to belong to this task, preserving siblings and tenants.
  State irreversible actions beforehand; ask when their scope or an architectural,
  public-contract, security or cost decision is unresolved.
- A refused tool call is a stop. Report it and wait; do not reshape or reroute it.
- Local backends use local databases/queues and development credentials. Viewing
  production through a local frontend does not authorize local operator credentials.
  Local Compose state may be reset for development.
- Authorized platform deployments use AWS `default`; disposable provider
  acceptance checks `default-test` first. List profiles and verify STS identity.
  Never switch deployment accounts to bypass a failure or copy credentials into
  workloads/images/GitHub secrets. CI deployments use OIDC.
- Use a task branch and one PR for related implementation, cleanup and fixes.
  Finish review/checks before shipping; merge only after checks pass and deploy
  related work together once.
- Work sequentially by default. Delegate only disjoint owners/files when justified;
  review returned work and integrated evidence. Verify tool and agent claims.
- Finish the requested scope and proportionate acceptance, then stop. Do not
  begin unrelated audits. Never add commit attribution trailers.
- Respond briefly and lead with outcomes. Name blockers and decisions explicitly;
  report build/CI/deployment success or failure without routine narration.
