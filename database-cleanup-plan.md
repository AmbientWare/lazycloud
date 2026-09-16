# Database cleanup and reset plan

Status: implementation in progress. No installation has been reset or deployed.
Branch: `refactor/relational-database`, based on `a257b2e18`.

## Outcome

Make PostgreSQL's schema describe the durable product state directly. Remove unused
tables and services, eliminate serialized copies of entire records, and retain JSON
only where the data needs it. Ship one explicit initial Alembic revision and rebuild
the selected installation from an empty database during a planned maintenance window.

The owner has authorized a full database reset and replacing the migration
history for this change, with no requirement to preserve existing application data.
The administrator's GitHub account ID is already configured. This is a one-time
exception to the frozen-baseline rule.
Identify the exact installation and affected resources before executing it. Future
deployed schema changes return to forward migrations.
Both production and local Compose are selected for the reset after acceptance.

There are 81 application tables and 42 tables with a `payload` column at this commit.
Those 42 are not all equivalent: for example, the app mapper already uses explicit
columns for app state and stores only app metadata in `payload`. Review actual field
ownership rather than treating every JSON column as a copy of the record.

## Design rules

- Store IDs, tenant ownership, relationships, states, timestamps, money, quantities,
  retry counters, leases, and fencing tokens in typed columns. Enforce material
  invariants with foreign keys, unique constraints, checks, and transactional updates.
- Use child tables for repeated data with its own identity, lifecycle, constraints,
  or query requirements. Do not create a table for every nested configuration object.
- Allow named JSONB columns for bounded user metadata, opaque invocation data, or
  variable configuration that is consumed as a document. Each retained column needs
  an owner, validation, a size policy, and a reason it cannot be ordinary fields.
  Application-controlled documents use precise versioned Pydantic contracts where
  versions are needed. Arbitrary user data stays distinct from control state.
- A named `config`, `metadata`, or `provider_state` column must not become a new home
  for all the old fields. Extract anything used for authorization, joins, scheduling,
  reconciliation, billing, cleanup, or regular filtering. Reconstruct transport
  objects from canonical stored values rather than persisting conflicting copies.
- Store secrets encrypted and tokens hashed through the existing owners. Do not
  expose them while inspecting records or measuring returned bytes.
- Keep deliberate historical snapshots, including charged rates and billing
  attribution. They describe past facts and must not follow mutable current rows.
- Use explicit row-to-domain mapping and targeted repository operations. Remove
  generic payload stores and reflection-based field copying once their callers move.
  No dual writes, fallback reads, compatibility wrappers, or alternate backends.
- Preserve public behavior, including omitted versus zero, tenant isolation, retry
  semantics, and history after deletion. Synchronize shared HTTP contracts, SDK,
  CLI, runner, and web schemas if a public boundary must change.
- Choose indexes from actual queries and retention behavior. Read narrow projections,
  aggregate in SQL, and keep recurring scans proportional to active or due work.

Table count is not an acceptance target. Four removals would leave 77 application
tables before any justified splits or further removals.

## Work sequence

Complete and validate one owner slice at a time on the implementation branch. Keep
the branch undeployed until the reset release is ready; do not mix old and new
application versions against the rewritten schema.

### 1. Finish the field and consumer inventory

For every table, record its production writers, readers, ownership, lifetime, and
retention policy. For each persisted record field, record its destination: existing
column, new column, child relation, justified JSON document, derived value, or deletion.
Include defaults, nullability, units, uniqueness, foreign keys, and delete behavior.

Trace consumers outside repositories too: triggers, raw SQL, JSON expressions,
scheduler scans, billing calculations, worker messages, admin commands, runbooks,
bootstrap jobs, and tests. Capture representative current query counts and returned
bytes for changed recurring paths before editing them, using synthetic history and
production repositories locally.

This inventory is the reviewable schema design for each slice. Resolve any proposed
public behavior or top-level ownership change before implementing that part.

### 2. Remove dead paths completely

The code review identified these candidates. Recheck production consumers and
references against the implementation branch:

| Table | Removal scope |
| --- | --- |
| `workspace_storage` | Remove the table, `workspaces.storage_id`, its foreign key, and obsolete cleanup references. Move the real workspace storage configuration out of the workspace record payload into explicit fields or an independently justified relation. |
| `pod_processes` | Remove the unused accessor and record type with the table. Preserve the working pod execution and URL services. |
| `routes` | Remove the unused SQL route repository, service, and composition. Keep route publication and lookup through their current Redis owner. |
| `queue_messages` | Remove the unused SQL collection service, repository, composition, and scheduler protocol. Resolve the obsolete `cron_job_runs.message_id` relationship and every public consumer. Preserve the current Redis queues and scheduled function execution. |
| `credentials` | Exact class references have no production consumers. The active compute join credentials and provider authorization records have separate owners and remain. |

Remove tests that exist only to exercise these retired implementations. Additional
removals need evidence that the production behavior no longer depends on them; an
empty table alone is insufficient. Optional provider and recovery features can have
empty tables while still being necessary.

### 3. Refactor the remaining payload tables by owner

These groups cover all 38 retained payload tables. Already relational tables remain
in the inventory for constraints, relationships, and query review.

1. **Identity and workspace settings.** `workspaces`, `workspace_members`,
   `workspace_audit_events`, `concurrency_limits`, `credentials`, `custom_domains`.
   Make workspace storage settings and credential lifecycle explicit. Remove unused
   member payload state. Preserve audit details only where they are actual event
   details. Follow the existing explicit identity mappers for users and tokens.
2. **App definitions and schedules.** `apps`, `stubs`, `deployments`, `cron_jobs`,
   `cron_job_runs`. Separate runtime configuration from mutable lifecycle state.
   Extract scheduling inputs from nested config where queried. Preserve immutable
   deployment configuration and valid user metadata without duplicating columns.
3. **Execution.** `tasks`, `task_attempts`, `task_dependencies`, `containers`.
   Make assignment ownership, retries, resource requests, cleanup state, and
   terminal results explicit. Review `containers.scheduling_request` alongside
   `payload`. Update database triggers and indexes that currently inspect JSON.
   Keep opaque user inputs/results separate from execution control fields.
4. **Compute and orchestration.** `compute_units`, `compute_capacity_operations`,
   `compute_provider_instances`, `compute_join_credentials`,
   `compute_machine_enrollments`, `workspace_compute_policies`,
   `aws_account_connections`, `aws_authorization_cleanup_tombstones`, `machines`,
   `workers`, `agents`, `agent_leases`, `autoscaler_states`.
   Model capacity, enrollment, readiness, claims, and cleanup directly. Review
   `provider_state`, runtime lists, and handoff data too. Preserve provider-specific
   details only behind their actual provider owner. Distinguish durable restart
   recovery from transient Redis state before moving or deleting any field.
5. **Storage and images.** `objects`, `volumes`, `images`, `image_archives`,
   `image_builds`, `checkpoints`. Make authorization, object identity, publication,
   retention, accounting cursors, and cleanup claims explicit. Keep shared archive
   ownership separate from workspace image access. Audit build dispatch documents
   and checkpoint metadata for duplicated control fields.
6. **Observability and usage.** `events`, `logs`, `worker_events`, `usage_records`.
   Store attribution, time, event kind, measurements, and units directly. Retain
   bounded event details and labels when needed. Preserve usage-to-ledger identity,
   monetary precision, and historical attribution. Verify retention is implemented
   and scheduled for data intended to expire.

Keep the existing relational billing, outbox, cleanup, dispatch, and authorization
tables where they protect distinct durable transitions. Simplify real redundancy,
but do not merge payment history or recovery records merely to reduce table count.

### 4. Replace the migration history and update bootstrap

- Create one explicit DDL baseline with a new revision identity, such as
  `0001_relational_baseline`, and `down_revision = None`. Remove the old revision
  chain only in this reset release. Do not reuse the old `0001_initial` identity:
  an old database must not appear current under a different schema.
- Build the baseline incrementally with each completed owner slice and finalize it
  after the schema settles. Recreate only disposable acceptance databases during
  development. No runtime `create_all`, auto-reset, or stamping old data as current.
- Preserve required extensions, triggers, exclusion constraints, partial indexes,
  and foreign-key ordering. Replace old JSON-dependent trigger logic explicitly.
- Separate schema creation from required seed/bootstrap work. Review old migrations
  for catalog or default data that a fresh installation still needs.
- Keep bootstrap idempotent and reject nonempty incompatible databases before any
  mutation. Remove historical upgrade-only tests for the discarded chain while
  retaining coverage of current production invariants.
- Update root and database `AGENTS.md` guidance and the deployment runbook to explain
  this one reset and the new frozen baseline. Preserve their `CLAUDE.md` symlinks.

### 5. Prove the result locally

Use real PostgreSQL and Redis through the production owners. Run changed-owner
formatting, type checks, and existing focused tests after each coherent slice.
Add a test only for an otherwise unproven material invariant.

Required evidence before reset:

- Fresh database bootstrap, repeat bootstrap, and rejection of an old incompatible
  database. Existing schema comparison must cover the new baseline's columns,
  defaults, keys, checks, and indexes; exercise material trigger behavior too.
- Administrator bootstrap linked to the intended GitHub identity, dashboard login,
  ordinary user restrictions, workspace isolation, and device/API token behavior.
- App deploy/update/delete, scheduled invocation, dependency/retry/cancel behavior,
  pod lifecycle, logs, and current Redis queue operations using existing exact named
  acceptance scenarios selected for the changed boundaries.
- Image build/reuse, volume and artifact access, retention, and scoped cleanup.
- Usage pricing, credit settlement, webhook idempotency, payment recovery, and
  deletion with retained accounting history through existing owner evidence.
- Idle and active query counts and returned bytes with representative retained
  history. Compare against the captured baseline, including replicas and cadence;
  investigate regressions rather than adding indexes without query evidence.

Audit remaining JSON by field, not by renaming columns. Demonstrate that SQL filters
and returned records read the same canonical state. Remove the generic payload
mixins/stores once no live owner needs them. Run the release gate once the integrated
release is complete. External credentials that are unavailable remain explicit
acceptance gaps, not mocked successes.

### 6. Execute the selected installation reset

This stage destroys the selected database's history. The reset authorization does
not establish which installation to target or authorize deleting unrelated external
resources. Before executing, supply the exact deployment/database identity and a
resource disposition list. Do not put credentials or raw customer records in it.

1. Prepare the tested release and a repeatable empty-database bootstrap. No data
   migration, export, backup, or restoration of old application records is required.
   Recovery from a failed reset is to fix the new release or rebuild the empty
   installation, with admission kept closed until it works.
2. Inventory current jobs, containers, workers, cloud instances, domains, storage
   objects, and external billing relationships while their ownership is still known.
   Decide explicitly what to retain, reattach, or retire through existing owners.
   A blank database does not stop cloud costs or remove storage objects. Resolve
   existing subscriptions and delayed payment events before replacing account IDs.
3. Close admission and pause deployment automation that could restart old services.
   Drain work and resolve pending billing/cleanup operations. Stop schedulers,
   workers, API writers, and bootstrap jobs before the destructive step. Maintain a
   controlled strategy for incoming webhook retries.
4. Clear only this installation's stale Redis jobs, leases, sessions, and cached
   authorization. Retire or reenroll old workers and replace affected service
   credentials through canonical bootstrap. Preserve unrelated secrets, provider
   resources, and trust material. Never flush a shared Redis instance blindly.
5. Reset only the identified database/schema, then run the new migration and normal
   identity/billing bootstrap with the new release. Use the already configured numeric
   GitHub user ID and verify it links to the administrator. Ordinary first login
   creates a member; it does not grant administrator access. Publish required catalog/rates before
   admitting users, and confirm the deployment's workload credentials still work.
6. Verify admin login, deploy/invoke, logs, storage, billing visibility, and cleanup
   using a small named workload. Poll database state, process logs, and provider
   state together. Confirm no old workload is charging or reconnecting unexpectedly.
7. Reopen admission and deployment automation only after those checks pass. Check
   deployed query rates under the new release. If startup fails, keep admission
   closed and repair or repeat fresh bootstrap, accounting for any external changes.
   Never start old code against the new schema.

## Delivery and completion

Develop the implementation on a dedicated feature branch with reviewable owner
commits and one coordinated reset release. Open a PR, pass its required checks,
and merge before the selected deployment procedure. Avoid unrelated product changes.

Done means the dead paths are gone; durable control state has one relational owner;
every remaining JSON field has a documented purpose; existing product workflows and
failure recovery pass; the fresh schema matches its baseline; and the selected
installation has working administrator access and no unaccounted external resources.

The exact deployment target and external resource dispositions are execution inputs,
not blockers to writing this plan or implementing and validating the refactor locally.

## Implementation evidence

Five obsolete tables and their unused services have been removed. Active routing and
queues use Redis. The SQL routing fallback had an indirect consumer; that consumer
now reads the active Redis route owner directly.

Workspaces, memberships, audits, concurrency limits, custom domains, apps, deployments,
cron schedules, cron runs, logs and events have explicit mappers. Workspace storage
settings are columns. Deployment specifications remain validated immutable documents.
App metadata and event details remain named JSON documents. Event container attribution
is a column used directly by its lookup. Log attribution remains protected by a trigger.
The unused cron queue, invocation payload and SQL message ID fields are removed from
the shared contracts and scheduler. Cron names are unique per workspace.

Scoped PostgreSQL/Redis evidence so far:

- Schema bootstrap, repeat bootstrap, incompatible revision rejection, and metadata
  comparison pass against the explicit new baseline.
- Identity ownership, custom-domain isolation and workspace deletion: 24 checks pass.
- Deployments, version history, app lifecycle and scheduler: 73 checks pass.
- Log attribution, ingestion, retention, usage pricing and scheduler: 72 checks pass.
- Changed identity, deployment, scheduler, execution and observability source files
  pass the scoped type checks performed so far.

Query measurements use production repositories with 202 synthetic records in an
isolated PostgreSQL database. Bytes count serialized returned rows, not wire traffic.
Before measurements use `a257b2e18`; after measurements use this branch.

- Custom-domain list with 200 deleted and two active records: one query in both;
  returned rows 202 to 2; bytes 142,686 to 484. Active reconciliation remains one query
  and two rows, bytes 1,298 to 484. Idle reconciliation remains one query, zero rows.
- Logs with 200 old records and two in the requested window: one query and two rows
  in both; bytes 1,992 to 942. Idle follow query returns zero rows in both.
- Events under the same history: one query and two rows in both; bytes 1,420 to 984.
  Idle query returns zero rows in both.

Tasks, attempts and dependencies now use explicit columns. Opaque inputs and
results remain documents. Retry settings have checked columns, and commands and
retry statuses use PostgreSQL arrays. Runtime limits, scaling policy and artifact
references are explicit stub fields; configuration documents retain the remaining
authored launch declarations. Unused stub aliases and version fields are removed.
Configuration fingerprints normalize equivalent integer/float values so database
round trips preserve revision reuse. Existing reuse coverage includes a timeout.

- Task retry, timeout, claim, cancellation, progress and rerun: 15 checks pass.
- Stub preparation, concurrent reuse, cloning, artifact access and deployment
  defaults: 22 checks pass.
- Schema, workspace deletion races and scheduler integration after these changes:
  73 checks pass. Database, control and observability packages pass type checking.
- Task lists with two pending tasks and 200 finished tasks: one query, two rows;
  serialized bytes 2,274 to 1,158. Idle scans remain one query and zero rows.
- Autoscaler projections for two selected stubs: one query, two rows; serialized
  bytes 332 to 468. The 136-byte increase comes from separate nullable policy
  columns replacing compact empty policy objects. It adds no query or fetched
  configuration document. Empty target sets issue no query in either version.

Usage records now store attribution, units and metering instants in explicit
columns. Worker events retain only event details in their named document.
Usage producers put attribution in labels; metadata no longer supplies a second
attribution source. Remaining labels describe diagnostic dimensions and reported
allocations; durable placement rows remain the authority for billing reservations.
Malformed metering timestamps remain diagnostic evidence and cannot establish a
chargeable interval.

- Schema, workspace deletion, usage, billing costs, reconciliation and worker
  supervision: 45 checks pass. Scoped type checks pass.
- Usage with 200 old records and two in the requested window: one query and two
  rows in both; serialized bytes 1,602 to 1,106. Grouped usage remains one query,
  one row and 77 bytes. Both idle queries return zero rows.

Image authorizations, archives, builds and checkpoints use explicit mappers and
columns. Image recipes remain validated authored documents with their context
object reference extracted. Build cache details retain publication diagnostics;
cleanup paths, cache keys, asynchronous execution and archive version/state are
columns. Bounded build diagnostic lines and checkpoint ports use arrays. Full
ordered build output remains in `image_build_logs`. The unused duplicate build
container ID is removed. Dispatch retains the immutable prepared execution message
needed to retry submission; mutable dispatch ownership and timing are columns.

- Schema, artifact retention, workspace deletion and app execution summaries:
  33 checks pass. Image planning, dispatch and publication checks pass.
- Dispatch with 200 completed and two pending builds keeps the same statement
  counts, including transaction setup and claim updates: two idle, three active.
  Idle reads return zero rows. Active reads return two rows, with serialized
  bytes reduced from 7,620 to 288 by selecting only dispatch fields.

Containers, compute and storage still require their remaining refactors.
These scoped results are not release acceptance.
