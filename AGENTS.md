# Repository rules

Read [update.md](update.md) before planning or changing an owner. Follow its target
architecture, product baseline and completion requirements. Resolve
conflicts before implementation. Keep task plans and progress with the task.

Edit AGENTS.md, never its CLAUDE.md symlink. Add that symlink beside every new
guidance file. These files contain development standards and constraints.

## Ownership and implementation

- Build backend and host runtime code in Go. Keep the public SDK, CLI and
  Python runner in Python, and the frontend in TypeScript.
- Name code for its responsibility: execution, scheduler, agent. No GoRuntime,
  go_backend, new_api, V2 or legacy wrappers in product code. Actual public
  protocol versions are distinct from implementation migration labels.
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
- PostgreSQL owns durable state; Redis owns transient coordination and rebuildable
  projections; object stores and filesystems own bytes. No duplicate authorities.
- Use one production implementation. No fake success, weaker test backends,
  compatibility shims or fallback implementations. Delete superseded paths.
- SDK and runner remain independent of backend implementations. Cross-language
  contracts are language-neutral; Python code serialization stays explicit.

## Development

- Use one root Go module with a pinned `toolchain` directive, added with the
  first Go code. Pin code generators and linters as `tool` dependencies in
  go.mod. Keep dependencies small and justified. Use gofmt, go vet,
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
- Design new resource APIs and typed errors; backward compatibility is not
  required. Keep operation IDs stable within the new API, paginate collections,
  and coordinate changed contracts with SDK, CLI, runner and web consumers.
- Authorize each requested workspace against token scope and membership. Only an
  administrator may reach tenant workspaces without membership. Worker commands
  must also match current assignment authority.
- Separate wire/storage/domain types only where meanings differ. Do not copy
  every model into every layer. Update contracts and Python/runner/web consumers
  together; avoid hand-maintained parallel schemas.
- Use a fresh schema and SQL migration chain for the new platform. Freeze every
  revision once deployed. Old migration history remains in the pinned reference;
  do not import it or add compatibility bridges by default. Production resets
  and data imports require separately authorized scope.

## Acceptance

- Define the user-visible outcome and cheapest authoritative evidence first.
  Verify material invariants at owner/public boundaries. Use real services,
  containers and providers when those boundaries change.
- Retain tests only for unique proof of authorization, integrity, durability,
  concurrency, cleanup or public behavior. Do not test implementation shape,
  mock call order, wiring, snapshots or behavior already proven elsewhere.
- Owner tests live beside code; root tests cover cross-owner behavior. Do not
  import old backend code or E2E internals to make replacement tests pass.
- Run focused checks with visible output. Use gofmt, go vet, golangci-lint and
  targeted `go test -race` once Go code exists; use pytest -x for Python. Broad gates support
  releases or changes that span those owners.
- Measure admission/placement separately from capacity wait and user execution.
  Record affected latency, queries, bytes, round trips and contention with idle,
  active and growing-backlog workloads.
- Missing services or credentials are acceptance gaps, not permission for mocks.
  Name unverified boundaries and clean up every task resource.
- Deleting baseline code is preparation. Compare completed equivalent capability
  against the pinned reference before claiming simplification or performance.

## Safety and delivery

- Inspect dirty trees and preserve unrelated work. The reference checkout is
  read-only. Never copy its credentials, environment or runtime state.
- Local backends use isolated local databases, queues and development credentials.
  Do not inherit another checkout's .env. Reset only task-owned Compose projects.
- Inspect external systems before changing them. Delete only exact task-owned
  resources. State irreversible actions beforehand. Ask when architectural,
  public-contract, security, cost or destructive scope is unresolved.
- A refused tool call is a stop. Report it and wait; do not reroute it.
- Authorized deployments use AWS default; disposable provider acceptance checks
  default-test first. Verify profiles and STS identity. Never change accounts
  to bypass a failure or copy credentials into workloads. CI uses OIDC.
- Use a task branch and one PR per coherent change. Review and pass relevant
  checks before merge or deployment. Do not ship an incomplete replacement.
- Use the parallel-agent protocol in update.md when delegating. Assign disjoint
  owners/files after their contracts are agreed; one integrator owns shared
  definitions and root build files. Verify returned work and integrated evidence.
- Finish the requested scope, then stop. No commit attribution trailers.
  Report outcomes, blockers and unverified boundaries briefly.
