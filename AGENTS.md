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
- Use the single best production route. Do not add feature flags, capability
  gates, environment switches, or fallbacks that let test and production take
  different paths, and do not silently degrade to a weaker backend. A path only
  production takes is a path only production debugs. Where a capability is
  genuinely unavailable, fail loudly and name the reason.
- Protect secrets and user work. Never expose secrets in output, URLs, logs,
  tests, comments, docs, or durable records. Inspect the dirty tree, preserve
  unrelated changes, and stage only intentional files.
- Prefer the complete long-term fix within scope. Do not absorb adjacent cleanup
  unless it directly blocks the requested outcome or prevents a security,
  data-loss, paid-resource, concurrency, or cleanup failure.

`../beta9` is the reference implementation and is always available to read—for a
requested capability or public workflow, and equally for infrastructure and
architecture shape: how a concern is scoped, what the durable model looks like,
where ownership sits, how storage and deployment are laid out. Consult it before
designing something substantial rather than after, and say what it does when
proposing a design.

Treat it as the baseline to judge against, in both directions. Building more
than beta9 needs a reason named in the change—a capability we already have and
would otherwise regress, or a security, durability, or cost property it does not
provide. Building less needs the same. Matching it by default is the cheapest
correct answer, and a design markedly more complex than beta9's is a signal to
re-check the requirement, not a sign of rigor.

All implementation here must be original. Match production capability, security,
durability, operability, cost, performance, and public contracts—not internals
or names. Bot remains out of scope.

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

Give a test run a short timeout and extend it only when a real result needs the
time. Unit suites finish in seconds, so a long timeout does not make a slow run
succeed—it hides why it was slow. A generous limit turns a test that blocks on
an unreachable dependency into a wait instead of a finding, and discards the
signal that something reaches outside its owner at all. Keep the output
observable rather than piping a long run to `tail`, and prefer fail-fast
(`pytest -x`) with narrow owner scopes so the first real failure surfaces
immediately.

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

## Reporting

Answer condensed. This is a must-follow rule, not a preference.

- Lead with the answer or outcome. State blockers and decisions needed in one
  line each.
- Omit reasoning already accepted, alternatives not taken, restated context,
  and evidence the reader did not ask for. Link or name a file, ticket, or
  command instead of reproducing its content.
- No recap sections, no narration of what was just done, no tables or headings
  unless they carry information prose cannot.
- Expand only when asked, or when a correctness, security, cost, or data-loss
  risk needs the detail to be actionable.

## Work And Collaboration

GitHub Issues track work; the `ticket` label marks a tracked ticket. Open one
only for substantial big-ticket work that genuinely needs tracked design,
ownership, dependencies, or multiple acceptance checkpoints. Small fixes,
subtasks, contained implementation, docs/config changes, and incidental
follow-ups proceed directly without a ticket, even when discovered during
tracked work.

Work continues under the ticket that owns it until that ticket is complete. Do
not open a new ticket for follow-up, remaining scope, or a blocker discovered
inside tracked work—record it as a comment on the owning ticket and keep going.
Open a separate ticket only when the work has a genuinely different owner or
outcome and would stand alone. Comments record outcome, decisions, blockers, and
concise evidence—not command diaries. The issue body stays the current
description of the ticket; edit it when scope changes rather than appending
corrections.

Ticket work uses a branch per ticket. Rebase, validate a clean feature tree, and
merge through a pull request that references the ticket and closes it. A ticket
is done when its acceptance passes and its pull request merges; move it on the
board rather than restating the outcome in a file.

Tickets live on the `Agent Development` project board and advance through
`Todo`, `In progress`, `Under review`, `Merged`, in that order. Move the ticket
yourself as its real state changes—to `In progress` when work starts, to
`Under review` when its pull request opens, to `Merged` when that pull request
merges. Never skip a column or move a ticket backwards to make the board agree
with a mistake; correct the work instead. Board automation is a safety net for
the states it can observe, not a substitute for moving the ticket.

## Working Rules

Default to one task at a time. Parallel work is the exception you justify, not
the mode you assume: it requires genuinely disjoint owners and files, and the
manager still reviews returned work and runs integrated acceptance. Delegating
does not reduce the number of things you are responsible for finishing. Agents
stay within assigned files, preserve concurrent changes, surface real blockers,
and report changed paths, evidence, gaps, and conflicts.

A task's scope is fixed when it starts. Work discovered along the way—a defect,
a stale contract, a better design—is recorded on the owning ticket and left
there. Absorb it only when it blocks the requested outcome or prevents a
security, data-loss, paid-resource, concurrency, or cleanup failure, and say so
when you do. Three reasonable-looking widenings in a row still end somewhere the
owner never agreed to go, so when scope grows, stop and get agreement rather
than proceeding under an assumption.

Finish the current task before starting the next. A task that reveals a larger
problem is still the task you finish; the larger problem gets a ticket. Leaving
a tree that does not build or a change half-migrated across owners costs more
than the work saved.

A returned result from a subagent, a tool, or a prior run is a claim with
evidence attached, not an established fact. Verify anything that would change
what you build, delete, or tell the owner. Report what you actually observed and
name what you did not.

Resolve a destructive target exactly before acting on it. List what a delete
would remove and confirm every item belongs to the current task; a stack, a
bucket, or a prefix is not self-describing. Prefer the reversible step, and when
an action is irreversible, say so plainly before taking it rather than after.

Match the check to the change: iteration checks while editing, changed-owner
checks after a coherent slice, and the broad gate only for a release or an
explicit quality claim. A green narrow run is evidence for the owner it covered
and nothing more.

Finish when the requested outcome and proportionate acceptance pass. Do not
start a new audit or broad hardening pass without a concrete in-scope reason.
Never add commit attribution trailers; commits are authored by the repository
owner.
