# Repository guidance

Every `AGENTS.md` has a `CLAUDE.md` symlinked to it. Edit the `AGENTS.md`; never
write through the symlink or replace it with a regular file. A new `AGENTS.md`
gets its `CLAUDE.md` symlink in the same change.

## Core rules

- Fix the architecture, model, boundary, ownership, or signature, not the
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
- Read other codebases freely and learn from them: how a problem was solved,
  what a design costs, what it missed. Match the capability, security,
  durability, operability, cost, performance, and public contracts production
  requires rather than another project's internals.

## Reporting and responding to the user

Answer condensed. This is a must-follow rule, not a preference.

- Lead with the answer or outcome. State blockers and decisions needed in one
  line each.
- Omit reasoning already accepted, alternatives not taken, restated context,
  and evidence the reader did not ask for. Link or name a file or command
  instead of reproducing its content.
- No recap sections, no narration of what was just done, no tables or headings
  unless they carry information prose cannot.
- Expand only when asked, or when a correctness, security, cost, or data-loss
  risk needs the detail to be actionable.

## Ownership and architecture

- `packages/shared` owns backend-free boundary contracts and protocol-neutral
  types and helpers. JSON contracts live under `shared.http.*`.
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

## Style and tooling

Keep dependencies explicit and owner-directed. Use Python 3.12 types, Pydantic
v2 at runtime boundaries, precise domain enums/dataclasses/protocols, and
selective exports. Production defaults belong in shared contracts or backend
normalization; preserve meaningful distinctions such as omitted versus `0`.

Work from the repository root with `uv`; Bun is the only web package manager.
Use `apply_patch` for manual edits and Ruff for Python formatting/imports.

Comment sparingly, and only about the code as it now stands. A comment earns its
place by explaining what the code cannot say itself: a non-obvious constraint, an
ordering that must hold, a rejected alternative that looks correct. Do not narrate
what the next line does, restate a name, or describe a change relative to what was
there before. The reader has the current code, not the diff, and a comment about
"used to" or "now" is stale the moment it is written. Rationale that belongs to a
change belongs in the commit message; rationale that belongs to a decision belongs
in the owning `AGENTS.md`. Delete comments that no longer describe the code when
you touch the surrounding lines.

Writing for people follows the `unslop` skill. Load it before writing or editing
an `AGENTS.md`, documentation, a commit message, or a reply.
If it is not installed, install it first from
https://github.com/cursor/plugins/blob/main/pstack/skills/unslop/SKILL.md
(for Claude Code, as `~/.claude/skills/unslop/SKILL.md`), then load it.

## Public boundaries

- Resources use `/api/v1/<resource>`; `/gateway/*` is reserved for RPC-style
  control. A bearer token names an account or one workspace; a request names the
  workspace it acts on and is checked against membership, and only an
  administrator reaches a workspace they do not belong to.
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

## Acceptance and tests

Define the user-visible outcome and cheapest authoritative evidence before
implementation. Complete the coherent owner or cross-owner slice before
validating it. Match the check to the change: iteration checks while editing,
the narrowest changed-file/owner checks once per slice, a real local service,
public workflow, container image, deployment, or provider only when that
boundary changed, and the broad repository/release gate only for a release or
an explicit broad quality claim, never as routine feature acceptance. A green
narrow run is evidence for the owner it covered and nothing more. Reuse healthy
infrastructure and clean up every process, port, and resource created for
acceptance.

Run tests directly and let them finish. Keep the output observable rather than
piping a long run to `tail`, and prefer fail-fast (`pytest -x`) with narrow owner
scopes so the first real failure shows up immediately.

### Never wait on a state, always poll

Waiting for a state to be reached is not allowed. A wait keyed on the outcome
is keyed on exactly the thing that does not happen when something is wrong: a
phase becoming `ready`, a row appearing, a worker registering. It consumes its
whole timeout and then reports nothing about why.

Poll instead, fast, and read several independent signals every cycle: the
durable record, the logs at both ends, the external system's own view, and
whether the request arrived at all. Print them whether or not they changed.
Fast cycles are the point. They are how a wrong turn shows up in seconds rather
than at a deadline. No progress after a cycle or two is a finding to
investigate immediately, not a reason to keep waiting; reach into the running
thing (`docker compose exec`, SSM, `journalctl`) rather than waiting for it to
report out.

A terminal state may still end the loop early, but it is never what the loop
depends on, and the loop always emits its signals on the way. A silent watcher
that returns "still pending" after five minutes has produced nothing; the same
five minutes of polling would have named the cause.

Tests are optional evidence, not a completion ritual or count target. Add one
only when it is the cheapest unique proof of a material contract, failure,
authorization boundary, durable transition, data-loss risk, concurrency
invariant, or cleanup obligation. Existing authoritative coverage or a
production-representative execution can be sufficient. Preserve focused
matrices only when rows protect distinct high-risk transitions.

### Test decision gate

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

Owner tests live beside their package/app; root `tests/` is for concrete
cross-owner or deployment behavior. Opt-in live scenarios live under
`tests/e2e/`, are excluded from ordinary test discovery, and run only through
an exact named module (`python -m tests.e2e...`) or browser node after cheaper
owner evidence passes. Do not execute Python E2E files by path or add
import-path bootstrap code.

An unavailable credential or external service is an acceptance gap. Report it
after exhausting meaningful local evidence; never replace it with a mock or
local-only backend. Fix failures caused by the change or blocking its outcome
and report unrelated failures separately.

A target the owner explicitly selects or supplies for a run is approved; do not
refuse it because its account or network name looks personal or shared. Treat
every external system a live run touches, whether a provider account, cluster,
tailnet, DNS zone, or registry, as shared state you do not own. Read its current
configuration before mutating it, scope every change to resources the run created
and can name exactly, preserve every unrelated user and resource, and prove
cleanup is equally scoped. Never replace a whole policy or configuration
document, and never delete or rotate a resource that is not proven to belong to
the current run.

## Product phase and destructive work

The repository is deployed. A persistent installation holds data nobody can
reconstruct, so a schema change adds an Alembic revision chained onto the one
before it and `0001_initial` is never edited or renamed again. Do not rewrite
the baseline: a deployed database records the revision it reached, and changing
the file it points at makes that record a lie and refuses the next deploy.
Local development state is the Compose databases, volumes, and stacks;
resetting and re-bootstrapping those is ordinary development work. Never reset
external, deployed, or production data.

Resolve destructive targets exactly before acting. List what a delete would
remove and confirm every item belongs to the current task; a stack, a bucket,
or a prefix is not self-describing. Preserve tenant/workspace scope, sibling
resources, retries, idempotency, fencing, partial-failure state, and cleanup
proof where relevant. Prefer the reversible step, and when an action is
irreversible, say so plainly before taking it rather than after. Stop for user
direction when an irreversible action, public contract, security/cost posture,
provider strategy, or top-level architecture choice is genuinely unresolved.

## Working rules

Develop each feature on its own branch, open a pull request, and merge it only
after its checks pass.

Default to one task at a time. Parallel work is the exception you justify, not
the mode you assume: it requires genuinely disjoint owners and files, and the
manager still reviews returned work and runs integrated acceptance. Delegating
does not reduce the number of things you are responsible for finishing. Agents
stay within assigned files, preserve concurrent changes, raise real blockers,
and report changed paths, evidence, gaps, and conflicts.

Finish the current task before starting the next. A defect the task reveals is
usually cheapest to fix in the same change, so take it there rather than
deferring it to a later pass. Leaving a tree that does not build or a change
half-migrated across owners costs more than the work saved.

A returned result from a subagent, a tool, or a prior run is a claim with
evidence attached, not an established fact. Verify anything that would change
what you build, delete, or tell the owner. Report what you actually observed and
name what you did not.

A value that leaves this process has consumers who decide what it means, and
they are the ones who define it. Before changing one, whether an enum member, a
status, a sentinel, or a default, read what reads it on the far side. A change
that looks like relabelling here is a behaviour change there, and the reasoning
that makes it look safe is written in the module you are editing, not in the one
that acts on it. The trap is the value that reads as a null: a placeholder
locally is a fact somewhere else, and the code that treats it as one is exactly
the code you have not opened. Grep for the consumers first; it is cheaper than
any of the ways of finding out afterwards.

A blocked tool call is a stop, not an obstacle to route around. When a permission
layer refuses an action, say what was refused and what it was for, and wait. Do
not re-issue it reshaped: split, re-encoded, moved into a script or a test, or
narrowed until it passes. Reshaping until something succeeds defeats the only
control the owner has over what runs, and it converts a decision that was theirs
into one already made. Continue with whatever genuinely does not depend on the
refused action, and name the rest as blocked.

Finish when the requested outcome and proportionate acceptance pass. Do not
start a new audit or broad hardening pass without a concrete in-scope reason.
Never add commit attribution trailers; commits are authored by the repository
owner.
