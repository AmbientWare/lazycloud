# Repository Guidance

Python 3.12 application. Read this file and the nearest nested `AGENTS.md` before
changing a file; nested guidance adds owner-specific constraints and does not
repeat this policy.

## Core Rules

- Fix the architecture, model, boundary, ownership, or signature—not the
  checker. Do not hide problems with `type: ignore`, broad `Any`/`object` or
  casts, checker-only branches, exclusions, shims, fallback imports, fake
  adapters, or compatibility wrappers.
- Implement current production behavior through canonical owners. Delete stale
  aliases, routes, stores, wrappers, and tests instead of preserving old paths.
- Do not create fake success paths. Acceptance uses the production
  implementation and contracts; local processes may use real Docker-backed
  dependencies where those are the production boundary.
- Protect secrets and user work. Never expose secrets in output, URLs, logs,
  tests, comments, docs, or durable records. Inspect the dirty tree, preserve
  unrelated changes, and stage only intentional files.
- Prefer the complete long-term fix within scope. Do not absorb adjacent cleanup
  unless it directly blocks the requested outcome or prevents a security,
  data-loss, paid-resource, concurrency, or cleanup failure.

Use `../beta9` only to understand a requested capability or public workflow.
All implementation here must be original. Match production capability,
security, durability, operability, cost, performance, and public contracts—not
internals or names. Bot remains out of scope.

## Ownership And Architecture

- `packages/shared` owns backend-free boundary contracts and protocol-neutral
  primitives. JSON contracts live under `shared.http.*`.
- `packages/lazycloud` owns the backend-free public SDK and `lazycloud` CLI.
- `packages/runner` owns code executed inside user containers and may depend
  only on shared contracts and user code.
- Domain packages own reusable decisions and services. Repositories map/query
  persistence; services decide workflows; handlers, commands, schedulers,
  workers, and process entrypoints stay thin.
- `apps/*` owns deployable composition and process lifetime. `apps/api` owns
  FastAPI composition, `apps/cli` owns internal `lazycloud-admin`, and
  `apps/web` owns the dashboard.
- Provider implementations live under `packages/providers/*` behind
  provider-neutral protocols.
- PostgreSQL/SQLAlchemy/Alembic own durable state. Redis owns queues, locks,
  leases, pub/sub, coordination, and short-lived caches. Object storage and
  mounted filesystems own object/file data. Do not add duplicate stores or
  backend switches.

Keep dependencies explicit and owner-directed. Use Python 3.12 types, Pydantic
v2 at runtime boundaries, precise domain enums/dataclasses/protocols, and
selective exports. Production defaults belong in shared contracts or backend
normalization; preserve meaningful distinctions such as omitted versus `0`.

## Public Boundaries

- Resources use `/api/v1/<resource>`; `/gateway/*` is reserved for RPC-style
  control. Bearer tokens scope workspaces; only admins may override workspace.
- FastAPI handlers validate and authorize, call one service, and map typed
  results. JSON routes use `HttpModel` contracts, precise response models,
  stable operation IDs, `{data, next}` lists, `datetime`, and `204` for
  bodiless success.
- Services raise typed `shared.errors`; central API handlers map them to
  `ErrorResponse`. SDK/CLI transport failures use `HttpApiError`. Do not add
  soft-error envelopes or unvalidated response dictionaries.
- Keep Pydantic contracts, SDK/CLI consumers, runner consumers, and hand-written
  web Zod schemas synchronized in the same change.
- CLI commands remain thin, preserve clean machine-readable output, show real
  progress, and never present polling or fabricated output as logs.

## Acceptance And Tests

Define the user-visible outcome and cheapest authoritative evidence before
implementation. Complete the coherent owner or cross-owner slice before
validation, then run the narrowest changed-file/owner checks once. Expand to a
real local service, public workflow, container image, deployment, or provider
only when that boundary changed. Reuse healthy infrastructure and clean up
every process, port, and resource created for acceptance.

Tests are optional evidence, not a completion ritual or count target. Add one
only when it is the cheapest unique proof of a material contract, failure,
authorization boundary, durable transition, data-loss risk, concurrency
invariant, or cleanup obligation. Existing authoritative coverage or a
production-representative execution can be sufficient. Do not test repository
instructions, source/import/export/route inventories, implementation shape,
mock transcripts, plans or generated commands as acceptance, compatibility,
literal presentation values, or behavior already proven elsewhere. Preserve
focused matrices only when rows protect distinct high-risk transitions.

### Test Decision Gate

Do not add a test merely because code changed, a bug was fixed, or a test could
be written. Before adding or retaining an automated test, all of these must be
true:

1. It proves current production behavior at a stable owner or public boundary,
   not test machinery or an implementation detail.
2. Failure would materially affect authorization, security, data integrity,
   durability, concurrency, cleanup, a public contract, or a user-visible
   terminal outcome.
3. The same invariant is not already proven at a cheaper authoritative owner or
   by the production-representative acceptance path.
4. The test is cheaper, more deterministic, and more maintainable than proving
   the behavior through the real owner or named acceptance scenario.

If any answer is no, do not add the test. Delete existing tests that fail this
gate when they are encountered in the changed scope.

Test stable production decisions such as validation and serialization at a real
boundary, authorization and tenant isolation, durable state transitions,
idempotency and fencing, data-loss prevention, concurrency invariants, scoped
cleanup, and regressions in production code that escaped existing authoritative
evidence and are likely to recur.

Do not test:

- E2E scenarios, smoke scripts, acceptance harnesses, runbooks, setup/teardown
  orchestration, fixtures, or their private helpers. Execute the exact named
  scenario instead.
- Call order, mock transcripts, constructor wiring, generated commands or
  plans, constants, signatures, types, source layout, imports, exports, routes,
  repository policy, or other implementation shape.
- Literal copy, colors, pixels, snapshots, provider inventory fields, volatile
  external configuration, or third-party defaults unless they are themselves a
  stable public contract with material user impact.
- Behavior already proven by an owner test, integration boundary, real service
  execution, public CLI/API/SDK workflow, or live provider acceptance.
- One-time deployment wiring or a fixed bug by default. Add evidence only when
  it independently passes the four-part gate above.

Make production behavior work first. Add at most the smallest focused case for
each unique invariant; do not create a matrix, shared fixture, helper framework,
or companion suite for a single scenario. If acceptance orchestration becomes
complex enough to seem unit-testable, simplify it. If it contains reusable
production decisions, move those decisions to their proper production owner
and test that owner instead. Never import `tests.e2e` internals into pytest
tests.

Work from the repository root with `uv`; Bun is the only web package manager.
Use `apply_patch` for manual edits and Ruff for Python formatting/imports.
Owner tests live beside their package/app; root `tests/` is for concrete
cross-owner or deployment behavior. Opt-in live scenarios live under
`tests/e2e/`, are excluded from ordinary test discovery, and run only through
an exact named module (`python -m tests.e2e...`) or browser node after cheaper
owner evidence passes. Do not execute Python E2E files by path or add import-path
bootstrap code.
Run the broad repository/release gate only for a release or explicit broad
quality claim, never as routine feature acceptance.

An unavailable credential or external service is an acceptance gap. Report it
after exhausting meaningful local evidence; never replace it with a mock or
local-only backend. Fix failures caused by the change or blocking its outcome
and report unrelated failures separately.

For live testing, whichever Tailnet the user selects or supplies for the run is
approved; do not reject it because its account name appears personal, shared,
or otherwise non-dedicated. Treat every provided Tailnet as shared external
state: inspect the current configuration before mutation, scope changes to
explicitly LazyCloud-owned test tags, grants, clients, keys, routes, and
devices, preserve every unrelated user and resource, and prove cleanup is
equally scoped. Never replace the complete Tailnet policy or delete or rotate a
resource that is not proven to belong to the current test.

## Product Phase And Destructive Work

The repository is predeployment until the owner declares the first persistent
production installation. SQLAlchemy metadata plus one reviewed Alembic baseline
define the schema. Update that baseline and use fresh PostgreSQL bootstrap; do
not build historical revisions, upgrade/downgrade paths, previous-binary
compatibility, or transition smokes. Recreate only databases positively
identified as local and disposable. Never reset unknown, shared, external, or
persistent data.

Resolve destructive targets exactly before acting. Preserve tenant/workspace
scope, sibling resources, retries, idempotency, fencing, partial-failure state,
and cleanup proof where relevant. Stop for user direction when an irreversible
action, public contract, security/cost posture, provider strategy, or top-level
architecture choice is genuinely unresolved.

## Work And Collaboration

Use `board/todo.md`, `board/loop.md`, and `board/finished.md` only for substantial
big-ticket work that genuinely needs tracked design, ownership, dependencies,
or multiple acceptance checkpoints. Small fixes, subtasks, contained
implementation, docs/config changes, and incidental follow-ups proceed directly
without board bookkeeping, even when discovered during tracked work. Board
entries record outcome, decisions, blockers, and concise evidence—not command
diaries.

While `findings.md` remains open, findings work integrates in the shared main
worktree with disjoint file ownership. Afterwards, board-managed code changes
use a feature branch/worktree recorded on the active task; rebase, validate a
clean feature tree, merge, and remove it after acceptance.

Parallel work is optional and only for clearly disjoint scopes. The manager
keeps ownership explicit, reviews returned work, and runs integrated acceptance.
Agents stay within assigned files, preserve concurrent changes, surface real
blockers, and report changed paths, evidence, gaps, and conflicts.

Finish when the requested outcome and proportionate acceptance pass. Do not
start a new audit or broad hardening pass without a concrete in-scope reason.
Never add commit attribution trailers; commits are authored by the repository
owner.
