# Repository rules

Guidance lives in AGENTS.md files, which Claude Code also loads; add no
CLAUDE.md beside them. These files contain development standards and
constraints. Keep task plans and progress with the task, not in the tree.

## Architecture

- Processes: `cmd/server` serves the public API, the host connection (gRPC)
  and the edge that forwards workload HTTP, WebSocket and TCP traffic.
  `cmd/scheduler` runs execution planning, placement, the cloud fleet, cron,
  callbacks, email and sweeps; replicas share passes through advisory locks
  and one elected leader runs the timers. `cmd/agent` runs on each host and
  supervises its containers. `cmd/supervisor` is PID 1 in every workload
  container and runs the language runner (`python/runner`) over the local
  runner protocol. `web/` is the dashboard.
- Owners are packages under `internal/`: control (apps, releases), execution
  (admission, attempts, container lifecycle, replica counts), scheduling
  (placement), compute (hosts, fleet, agent releases), identity, billing,
  storage, images, edge, schedules, secrets, observability, notifications.
  Each package comment names what it owns.
- PostgreSQL is the single durable authority. A transaction that changes
  state also calls `pg_notify`, and `internal/database` listeners wake the
  waiters and scheduler loops once it commits; leader timers catch work no
  notification announces. There is no Redis. Object stores and host caches
  hold bytes.
- Host runtime packages (agent, supervisor, diskengine) have no database
  access and reach the control plane only through the host protocol;
  depguard in `.golangci.yml` enforces it.
- Contracts live in `contracts/`: `openapi.yaml` (public API),
  `host/v1/*.proto` (host session, data and container link) and
  `runner.yaml` (local runner protocol). Go bindings come from `go generate`
  (oapi-codegen, buf, sqlc), Python models from `datamodel-codegen` profiles
  in pyproject.toml, TypeScript types from `bun run apigen`. Regenerate every
  binding in the change that edits a contract.
- Containers hold no platform credential. In-container SDK calls go to the
  container API socket the supervisor serves; the agent forwards each call
  as `ContainerAPI` tagged with the container, and the server authorizes it
  against the container's current assignment and workspace.
- `migrations/` is one ordered SQL chain; `server migrate` and server start
  apply it.

## Ownership and implementation

- Build backend and host runtime code in Go. Keep the public SDK, CLI and
  Python runner in Python, and the frontend in TypeScript.
- Name code for its responsibility: execution, scheduler, agent. No language,
  version or legacy labels in product names; public protocol versions such as
  `/v1` are the exception.
- Domain modules own decisions, workflows and transitions. Repositories own
  queries. Binaries own composition and process lifetime. Transports validate,
  authorize, invoke an owner and map typed results.
- Execution owns admission, workload scaling and container lifecycle; scheduling
  owns placement; compute owns machine capacity. Give each decision and state
  one owner. Keep cross-owner coordination explicit and small.
- Start with one package per owner. Add a package or process only for a concrete
  dependency, security, scaling, reuse or lifetime boundary. No package or service
  per entity.
- Prefer concrete dependencies. Define interfaces at the consumer, and only for
  actual substitution or external boundaries. Avoid service locators, giant dependency bundles, forwarding
  layers, speculative frameworks and abstractions used only by tests.
- PostgreSQL owns durable state and NOTIFY wake-ups; object stores and
  filesystems own bytes. No duplicate authorities. A cache or projection
  names its durable source and how it rebuilds.
- Use one production implementation. No fake success, weaker test backends,
  compatibility shims or fallback implementations. Delete superseded paths.
- SDK and runner remain independent of backend implementations. Cross-language
  contracts are language-neutral; Python code serialization stays explicit.

## Development

- Use one root Go module with a pinned `toolchain` directive. Pin code
  generators and linters as `tool` dependencies in go.mod. Keep dependencies small and justified. Use gofmt, go vet,
  golangci-lint and focused tests.
- Use typed identifiers and typed string constants for enums, checked for
  exhaustive switches. Never let a Go zero value stand for "omitted"; use
  pointers or explicit optional types where absence differs from zero.
  Distinguish absence, rejection and failure. Do not replace domain types with
  untyped maps or stringify errors before transport boundaries.
- Pass dependencies explicitly from main. No package-level mutable state and no
  init side effects. Share memory only behind a named owner; keep mutex scopes
  narrow and never hold one across network, disk or channel waits.
- Every goroutine has an owner that waits for it and a context that stops it.
  Bound queues, in-flight work and concurrency. Propagate cancellation and
  deadlines. Account for partial completion during shutdown and retries. Run
  tests with the race detector.
- Wrap errors with context (`%w`); match them with errors.Is/As, not strings. Do
  not ignore returned errors or panic for expected failure. Avoid unsafe and cgo
  unless a demonstrated need is documented. Memory safety does not prove
  authorization or distributed concurrency.
- `./check.sh` runs every formatter and linter as CI does; `--fix` applies the
  formatters first.
- Use uv from the root for Python, Ruff for formatting/imports and Bun for web.
  Preserve declared Python support, including Python 3.10+ for SDK and runner.
  Use Pydantic at Python wire boundaries.
- Use apply_patch for manual edits. Read consumers before changing contracts,
  statuses, defaults or ownership. Load unslop before writing prose. Comments
  explain current constraints. Describe LazyCloud directly.

## Data and contracts

- Filter, aggregate and project in SQL. Batch actual reads and writes. Recurring
  work scans relevant live or due resources, not retained history. Reuse scoped
  snapshots and retain authoritative admission and assignment checks.
- Name the invariant behind each transaction, lease and lock. Acquire ownership
  when work can execute, fence stale owners and keep retries bounded and fair.
  A batch failure must not discard successful results.
- Design resource APIs with typed errors. Keep operation IDs stable, paginate
  collections, and coordinate changed contracts with SDK, CLI, runner and web
  consumers.
- Authorize each requested workspace against token scope and membership. Only an
  administrator may reach tenant workspaces without membership. Worker commands
  must also match current assignment authority.
- Separate wire/storage/domain types only where meanings differ. Do not copy
  every model into every layer. Update contracts and Python/runner/web consumers
  together; avoid hand-maintained parallel schemas.
- Freeze every migration once deployed; add a new one for each change.
  Production resets and data imports require separately authorized scope.

## Acceptance

- Define the user-visible outcome and cheapest authoritative evidence first.
  Verify material invariants at owner/public boundaries. Use real services,
  containers and providers when those boundaries change.
- Retain tests only for unique proof of authorization, integrity, durability,
  concurrency, cleanup or public behavior. Do not test implementation shape,
  mock call order, wiring, snapshots or behavior already proven elsewhere.
- Owner tests live beside code; `acceptance/` covers cross-owner workflows
  against real PostgreSQL, Docker and the managed runtime.
- Run focused checks with visible output. Use gofmt, go vet, golangci-lint and
  targeted `go test -race`; use pytest -x for Python. Owner tests need
  `docker compose -f compose.test.yaml up -d --wait`. Broad gates support
  releases or changes that span those owners.
- Measure admission/placement separately from capacity wait and user execution.
  Record affected latency, queries, bytes, round trips and contention with idle,
  active and growing-backlog workloads.
- Missing services or credentials are acceptance gaps, not permission for mocks.
  Name unverified boundaries and clean up every task resource.
- Back simplification and performance claims with before/after measurements of
  the same capability.

## Safety and delivery

- Inspect dirty trees and preserve unrelated work. Never copy another
  checkout's credentials, environment or runtime state.
- Local backends use isolated local databases, queues and development credentials.
  Do not inherit another checkout's .env. Reset only task-owned Compose projects.
- Inspect external systems before changing them. Delete only exact task-owned
  resources. State irreversible actions beforehand. Ask when architectural,
  public-contract, security, cost or destructive scope is unresolved.
- A refused tool call is a stop. Report it and wait; do not reroute it.
- AWS default is the platform account: deployments and real EC2 checks of
  the platform fleet run there, with tagged, short-lived resources removed
  afterwards. default-test is the second account that connected-account
  checks need, not a sandbox. Verify profiles and STS identity. Never change
  accounts to bypass a failure or copy credentials into workloads. CI uses
  OIDC.
- Use a task branch and one PR per coherent change. Review and pass relevant
  checks before merge or deployment.
- When delegating, agree contracts first, then assign disjoint owners and files;
  one integrator owns shared definitions and root build files. Verify returned
  work and integrated evidence.
- Plan work that spans owners, PRs or parallel agents with PLANNING.md.
- Finish the requested scope, then stop. No commit attribution trailers.
  Report outcomes, blockers and unverified boundaries briefly.
