# Finished

Completed tasks move here with their title, original purpose, result, and concise acceptance
evidence. This is an outcome ledger, not a command diary; detailed history remains in Git.
`../findings.md` remains the authoritative checklist until every finding is checked.

## T-049 — Rebuild opt-in acceptance as scoped tests

Completed: 2026-07-22

Description: Audit every E2E, smoke, and acceptance-owned AWS/tool source; classify each as keep,
update, or delete; replace monolithic workflows with independently callable production scenarios;
and reorganize tests under clear owner, integration, local E2E, external E2E, browser, deployment,
and runtime boundaries.

Result: The 84-item review ledger is fully checked. The old root `e2e/` tree and general `tools/`
owner are deleted. Opt-in Python acceptance now lives under `tests/e2e/local/` and
`tests/e2e/external/`; browser acceptance lives with Playwright; deployment preparation lives under
`deploy/`; CI orchestration lives under `.github/scripts/`; and the shipped sandbox supervisor lives
under `apps/`. Fifty-three executable Python scenarios are independently selectable with exact
module commands, explicit live gates, blocked exit `77`, scoped production evidence, and public
cleanup. Connected AWS is eight stages, and only the separately authorized paid Function stage may
create compute.

Evidence: Ruff, formatting, and BasedPyright pass across the integrated migration; focused
deployment/SDK tests pass 21/21; sandbox-supervisor Go tests and focused web E2E ESLint pass.
Ordinary Pytest collection finds zero E2E nodes, and all 53 module preflights return `77` without
live authorization. Canonical Compose readiness and one real public Function invocation pass with
cleanup. The zero-cost AWS preflight correctly blocks before Tailnet mutation: public state still
projects three stale compute records and estimated hourly cost, while direct AWS evidence reports
zero EC2 instances, zero desired ASG capacity, and zero EBS volumes. T-033/T-005 own that production
state reconciliation before the paid stage.

## T-048 — Isolate deployable Docker image ownership

Completed: 2026-07-22

Description: Replace the root monolithic Dockerfile with durable image owners so ordinary
control-plane and storage iteration cannot build or ship the privileged worker toolchain.

Result: The root Dockerfile is deleted. Canonical files under `docker/` now own control-plane
services, the node agent, the container worker, and a minimal JuiceFS storage gateway. Compose has
one build owner per image; Helm, AWS Terraform, CI/release publication, Kubernetes/Tailnet
acceptance, and self-hosting docs use the same boundaries. The AWS installer’s stale Helm keys,
external-service Secret ownership, complete chart reconciliation hash, and preflight were corrected
while wiring the new image. BuildKit shares one ordinary Python download cache, and CI builds the
six control-plane targets as one family rather than duplicating maximum caches per image.

Evidence: All canonical targets build. On arm64, API fell from 533 MB to 464 MB, scheduler from
390 MB to 322 MB, cache from 222 MB to 157 MB, and worker from 969 MB to 898 MB. The dedicated
storage image is 210 MB on arm64 and 193 MB on amd64; clean Compose build output reaches only its
two storage stages, and inspection proves JuiceFS plus `nc` are present while Python, Docker,
Buildah, CRIU, runsc/runc, NVIDIA, Tailscale, Skopeo, and Mountpoint are absent. Worker inspection
proves its execution/storage/GPU tools and managed Python 3.10/3.11/3.12 catalog remain intact.
Compose format completed idempotently, gateway and WebDAV became healthy on retained volumes, and a
real authenticated WebDAV write/read/delete round trip passed. Bake/Compose ownership checks, Helm
lint/render, Terraform 1.9.8 format/validate, workflow parsing, Ruff, formatting, BasedPyright, and
30 focused tests pass. Temporary storage data was deleted; no AWS resources were contacted.

## T-042 — Keep local Compose administrator authority stable

Completed: 2026-07-22

Description: Make one private root `.env` administrator token the stable local development
authority across deliberate database resets without adding a Docker credential-extraction CLI.

Result: Compose mounts `LAZYCLOUD_TOKEN` only as a read-only secret into the one-shot administrator
bootstrap. Configured bootstrap accepts only canonical credentials, stores only the hash and prefix,
replays idempotently, fails closed on mismatch, and never publishes a second raw copy. Generated
bootstrap remains available when the setting is blank or absent. The public CLI reads the same root
`.env`; the legacy credential volume copy and all extraction helpers are removed.

Evidence: focused formatting, lint, typing, and identity/operator/deployment checks pass. The rebuilt
Compose bootstrap replayed the same administrator record, the public CLI authenticated directly from
the mode-`0600` root `.env`, and the recreated credential volume remained empty. Two fresh cycles of
an exact disposable PostgreSQL database used the production bootstrap image, authenticated the same
configured token, stored one administrator record, exposed no credential, and removed the database.

## T-046 — Apply all remaining strict test cleanup

Completed: 2026-07-21

Description: Apply the remaining strict decisions across the public SDK, other packages, root
suites, E2E, examples, and Go tooling while preserving active connected-AWS/capacity/auth work.

Result: All 217 DELETE definitions and 116 actionable UPDATE definitions were removed or replaced;
three concurrently rewritten UPDATE definitions were re-reviewed and retained as high-quality KEEP.
Seven focused replacements preserve generated-client, sandbox request, provider authority,
workspace ownership, failure cleanup, and Compose preflight contracts. Two neighboring duplicates
were absorbed into matrices, leaving the scope 328 definitions smaller. The Compose capacity path
was corrected to validate administrator authority before claiming its secondary worker.

Evidence: Scoped Ruff format/lint and BasedPyright pass; Go tests pass. The broad remaining-owner
selection passes 571 tests with 49 skips and 10 unrelated failures deselected; the overlap updates
pass 26 focused tests and the public Compose matrix passes five. The 10 full-selection failures are
unchanged retained gateway/operations/storage cases, not hidden by cleanup. Exact reconciliation,
final collection, and whitespace checks pass; nothing was staged or committed.

## T-044 — Apply strict app and shared-contract test cleanup

Completed: 2026-07-21

Description: Apply all 237 strict non-keep decisions under apps and shared contracts without
changing production behavior or overwriting active auth/source-cache/web work.

Result: All 105 DELETE and 132 UPDATE definitions were removed. Twenty-six UPDATE rows became 12
focused route, CLI-output, proxy-validation, persistence-default, execution-phase, timeline,
live-invalidation, and public-snippet tests; 106 rely on retained authoritative owner evidence. The
assigned scopes contain 225 fewer source definitions, with 38 empty modules deleted and zero blocked
or stale rows.

Evidence: Scoped Ruff format/lint and BasedPyright pass; Python acceptance passes 175 tests with one
skip. Web ESLint passes, all 122 Vitest tests pass, and reduced Playwright passes 16 tests with only
an existing nonfatal hydration diagnostic. Web typecheck is blocked solely by the pre-existing
`containers.test.ts` fixture missing `termination_reason`; route generation reports no other error.
Reconciliation and whitespace checks pass; nothing was staged or committed.

## T-047 — Simplify all agent guidance for fast, high-quality implementation

Completed: 2026-07-21

Description: Review every repository `AGENTS.md` and remove duplicated, procedural, and overly
prescriptive guidance while retaining rules that materially improve implementation quality.

Result: All 45 guidance files were rewritten. Total guidance fell from 1,843 lines and 12,874 words
to 624 lines and 4,489 words, reductions of 66.1% and 65.1%. Root is the 141-line canonical policy;
nested files now contain only owner-specific boundaries and risks. Boards are explicitly reserved
for substantial big-ticket tracked work; small fixes, subtasks, contained implementation,
docs/config changes, and incidental follow-ups proceed directly.

Evidence: All 45 final files were inspected, are headed/nonempty/newline-terminated, and pass
whitespace validation. Consistency searches confirm that board policy appears only in root and
blanket per-edit test/pre-commit mandates are absent. Architecture, secrets/user changes,
production-path acceptance, optional cheapest-unique tests, authorization, data-loss, concurrency,
destructive cleanup, and database safety remain explicit. No tests were needed and nothing was
staged or committed.

## T-045 — Apply strict compute, execution, image, scheduler, and worker test cleanup

Completed: 2026-07-21

Description: Apply all 314 strict non-keep decisions under compute, control, execution, identity,
images, scheduler, and worker while retaining material production-risk evidence.

Result: All 123 DELETE and 191 UPDATE definitions were removed. Nine repeated UPDATE rows became
four consolidated capacity-limit, RPC-retry, stop-failure, and scheduler-requeue tests; the other
UPDATE contracts are covered by retained authoritative owner evidence. All 473 audited KEEP
definitions remain. The assigned scopes contain 310 fewer source definitions, with 24 empty modules
deleted and no production changes.

Evidence: Scoped Ruff formatting and lint pass; BasedPyright reports no errors or warnings. The
combined owner run passes 234 tests with 79 environment skips after correcting one consolidated
matrix expectation, whose focused rerun passes. Reconciliation reports zero blocked or stale rows;
nothing was staged or committed.

## T-043 — Make every AGENTS test rule acceptance-driven and minimal

Completed: 2026-07-21

Description: Review every repository `AGENTS.md` and align test, validation, task-acceptance, and
completion guidance so tests are optional evidence rather than a procedural deliverable.

Result: All 45 guidance files were reviewed and 40 were updated. Root guidance now makes tests
optional unless they are the cheapest unique proof of a material production contract or risk;
`tests/AGENTS.md` owns the detailed strict quality bar; owner guidance retains only relevant domain
evidence. Security, authorization, data-loss, concurrency, and destructive-cleanup matrices remain
protected. The withdrawn manual pre-commit/pre-merge requirement and CI enforcement were not added.

Evidence: Guidance-only whitespace validation passes. Consistency searches found no remaining
procedural per-edit test mandate and no newly added pre-commit, pre-merge, or CI requirement. No
tests were run, and nothing was staged or committed.

## T-041 — Re-audit every test under a strict production-behavior quality bar

Completed: 2026-07-21

Description: Replace the conservative T-035 ledger with a source-level review that keeps only
unique production-behavior evidence and treats weak, duplicated, mock-transcript, implementation
shape, policy inventory, presentation literal, and plan-as-acceptance tests as cleanup.

Result: Root `tests-review.md` checks all 2,415 reviewed source definitions individually: 1,531
`KEEP`, 445 `DELETE`, and 439 `UPDATE`. Every non-keep entry has a concrete deletion, relocation,
consolidation, or production-boundary replacement action. A central overlap/risk pass corrected 65
owner-draft decisions, and three implementation-time concurrent rewrites were re-reviewed as KEEP,
including authorization, secret handling, durable cleanup, concurrency, CLI-output, and transport
evidence.

Evidence: Fresh framework-aware extraction found zero missing, stale, duplicate, blank, or
unchecked entries. Collection/listing reconciled 2,419 pytest, 259 Vitest, 70 Playwright, and 13 Go
executions (2,761 total). Audit and board whitespace validation passes. No test or production source
was changed by T-041.

## T-040 — Apply remaining audited package, root, integration, and E2E test cleanup

Completed: 2026-07-21

Description: Apply the 124 remaining T-035 decisions across root architecture, deployment,
integration, remaining package, example, and E2E owners.

Result: All 44 `DELETE` definitions are removed and all 80 `UPDATE` definitions are relocated or
consolidated, including 37 example contracts moved from root architecture scans to five example
test owners. No old target remains, no item is blocked, and active connected-AWS changes were
preserved untouched.

Evidence: Scoped Ruff and format checks pass. Focused acceptance passes with 247 tests plus 21
supplemental owner tests; 12 environment-dependent cases skip. The four initial rewritten-test
failures were corrected and 43 focused cases pass. Whitespace validation is clean.

## T-039 — Apply audited execution, image, scheduler, and worker test cleanup

Completed: 2026-07-21

Description: Apply the 139 T-035 decisions under execution, images, scheduler, and worker owners.

Result: All 10 `DELETE` and 129 `UPDATE` entries are implemented with zero blockers. Seven plan-only
files are deleted, retained contracts are consolidated into service/matrix tests, and scoped source
definitions fall from 701 to 580 without changing production code.

Evidence: Ruff and format checks pass; BasedPyright reports zero errors. Owner acceptance passes
with 537 tests and 110 environment skips after correcting one consolidated image-pool expectation.
The real-Redis replacement remains an honest environment skip rather than using a fake backend.

## T-038 — Apply audited app, web, SDK, and shared-contract test cleanup

Completed: 2026-07-21

Description: Apply the 98 T-035 decisions under apps, public SDK, and shared-contract owners.

Result: All 20 `DELETE` and 78 `UPDATE` entries are implemented with zero blockers or production
changes. Duplicate policy, compatibility, presentation, wiring, and matrix definitions are deleted
or consolidated while preserving public, security, redaction, and transport contracts.

Evidence: Ruff, format, BasedPyright, focused web ESLint, and Vitest pass. Python acceptance passes
with 215 tests and two environment skips; the affected correction rerun passes 49 tests with one
skip. The unrelated web-wide `termination_reason` fixture typecheck gap remains unchanged.

## T-035 — Review every repository test for unique production value

Completed: 2026-07-21

Description: Enumerate every concrete repository test definition and review each against the
production-contract, security, durability, cleanup, and minimal-test standards in `AGENTS.md`.

Result: Root `tests-review.md` contains 2,665 checked source definitions representing 3,004
collected executions. Decisions are 2,304 `KEEP`, 74 `DELETE`, and 287 `UPDATE`; every entry has a
reason and concrete action. No test or production source was changed by this audit.

Evidence: A fresh independent source extraction found zero missing, stale, or duplicate ledger
keys. Collection/listing reconciled 2,660 pytest executions, 261 Vitest executions, 70 Playwright
executions (35 source definitions across two projects), and 13 Go tests. The ledger has no unchecked
entries, and the audit and board diffs pass whitespace validation.

## T-034 — Reduce E2E tests to production-representative acceptance

Completed: 2026-07-21

Description: Remove low-value, duplicate, mock-lifecycle, presentation-matrix, plan-as-success, and
tool-inventory E2E coverage while retaining current production workflows and destructive safety
contracts.

Result: Superseded deployment, scaling, gateway-load, Playwright fixture-testing, panel-duplicate,
quality-matrix, and screenshot assets are deleted. Throughput ownership moved to the benchmark
harness. The browser collection is 35 distinct workflows across desktop and mobile, with current
shell, authorization, destructive actions, Tasks, storage, usage, onboarding, and marketing
coverage. The former `e2e/web_beta` harness is replaced latest-only by the read-only `e2e.web`
production gate. Provider validation exposes only lifecycle and cleanup; Tailnet preview and the web
gate report blocked, non-success outcomes without explicit live authorization.

Evidence: Focused Ruff, format, BasedPyright, shell syntax, Node syntax, and eight retained Python
safety/benchmark tests pass. Focused web E2E ESLint, all 261 Vitest tests, and Playwright collection
pass with 70 executions in 10 files. The web no-opt-in gate exits 77 and Tailnet preview exits 2 with
`ok: false`. Full web TypeScript validation was run and remains blocked only by pre-existing missing
`termination_reason` fields in two unrelated test fixtures. External AWS, Tailnet, Kubernetes, and
Compose acceptance was intentionally not run.

## T-031 — Own connected-AWS Compose identity and regional configuration

Completed: 2026-07-21

Description: Move the platform AWS identity, credentials, regional AMI discovery, and price inputs
out of the deleted acceptance harness and into stable Compose deployment ownership.

Result: A durable CloudFormation stack owns one named IAM control user whose only permission is
`sts:AssumeRole` on `compute-connection-*`. A typed, secret-safe Python operator command creates,
verifies, rotates, and revokes exactly that user's access key; preserves unrelated `.env` content
through mode-`0600` atomic replacement; validates current regional CPU/GPU images and the allowed
`i4i.xlarge` price; refuses ambiguous identity, credentials, permissions, scope, cost, and partial
release state; and supports one later catalog-plus-immutable-release activation. Root credentials,
release assets, customer authorization, and acceptance cleanup remain separate owners. Production
deployments continue to prefer workload or instance identity over this Compose-specific user.

Evidence: CloudFormation validation and deployment pass in `us-east-1`. The stack is
`CREATE_COMPLETE`, exactly one active key is present, `.env` references that exact key and user,
and the credential identifies itself through STS. A second configure is idempotent. IAM policy
simulation allows a matching managed-connection role and denies an unrelated role. Focused Ruff,
format, BasedPyright, provider-settings checks, and 19 tests pass, including redaction, rollback,
rotation ordering, exact revocation, invalid-release-before-mutation, partial-state refusal, and
single-write activation. The regional catalog was intentionally not persisted and Compose was not
restarted; T-030 owns the one atomic release/catalog activation after a clean committed checkpoint.

## T-029 — Retire the duplicate marketing examples route

Completed: 2026-07-21

Description: Remove the duplicate `/use-cases` marketing page while preserving the landing-page
example gallery and its future Mintlify entry controls.

Result: The `/use-cases` route, lazy page, page-only catalog copy, generated route registration,
prerender entry, and public-auth exception are removed. The landing gallery remains visually
unchanged. Its five cards, both Explore examples controls, and header/footer example destinations
remain visible with explicit disabled and Coming soon semantics until their Mintlify URLs are
configured; no control points at the retired internal route.

Evidence: Prettier on owned marketing files, focused ESLint, TypeScript and generated routes pass.
The production build prerenders only `/`, `/activate`, and `/dashboard`. Thirteen focused production
browser checks pass across desktop, standard and narrow phones, landscape phone, and tablet for
accessibility, responsive containment, disabled example controls, absence of dead `/use-cases`
links, card geometry, and header behavior. No commit was created.

## T-028 — Make the runnable examples the canonical marketing examples

Completed: 2026-07-21

Description: Replace generic marketing inventories with one shared five-example model for vLLM
inference, YOLO training, document OCR, sandboxed coding agents, and Parquet fan-out.

Result: The landing page and `/use-cases` share titles, summaries, stable anchors, guide paths, and
source-driven WebP artwork. The landing composition uses five compact portrait cards in one
full-desktop row, with each source image cropped into the upper 56%, its centered subject pulled
upward, a light paper fade through the midpoint, and title, summary, and arrow contained below.
Smaller desktop and mobile widths reflow rather than compressing the cards. Route transitions reset
the marketing scrollport, and anchored example links retain sticky-header clearance without a
duplicate offset.

Evidence: Prettier, focused ESLint, TypeScript, the production build and four-page prerender pass.
Thirteen focused production browser checks pass across desktop, standard and narrow phones,
landscape phone, and tablet for accessibility, containment, touch geometry, image/copy positioning,
the five-card desktop row, navigation, scroll reset, example anchors, and paper-surface rules. No
commit was created.

## T-025 — Publish a sandboxed coding-agent example

Completed: 2026-07-21

Description: Add an original coding-agent workflow with a trusted model planner and a separate,
network-blocked execution Sandbox.

Result: A real OpenAI-compatible planner Function returns a strictly bounded file replacement. The
local orchestrator revalidates it, uploads only approved files, runs the real seed test in a Sandbox
with no credentials or network, bounds returned output, and terminates the Sandbox in `finally`.

Evidence: Focused Ruff, BasedPyright, and 20 tests pass for provider boundaries, path/content limits,
public workload specs, real failing seed tests, process execution, and success/failure cleanup. Live
provider acceptance remains explicitly gated on a real provider credential and reachable control
plane; no fake production planner exists.

## T-023 — Publish a YOLO training and prediction example

Completed: 2026-07-21

Description: Add GPU-backed YOLO training and prediction Functions with durable artifacts and a
canonical guide.

Result: The example uses the current pinned official Ultralytics image, YOLO26n, a one-epoch COCO8
smoke, separate L4 training/prediction Functions, strict Volume-relative paths, and retained
checkpoints, metrics, labels, and annotated images.

Evidence: Focused Ruff, BasedPyright, and 16 tests pass for specs, path/symlink containment, typed
results, argument-vector execution, and dependency isolation. The guide records the authorized-L4
capacity/cost blocker instead of claiming a CPU or mocked GPU run.

## T-022 — Publish an OpenAI-compatible GPU service example

Completed: 2026-07-21

Description: Add an authenticated vLLM Pod with one GPU, a public model, durable cache, and a
canonical guide.

Result: The example pins the official vLLM image, explicitly restores its replaced `vllm serve`
entrypoint, serves Qwen through an authenticated OpenAI-compatible API, and persists model downloads
in a Volume.

Evidence: Focused Ruff, BasedPyright, and two spec/workflow tests pass. The guide covers health,
models, chat, replacement-cache reuse, cost, and teardown, and records GPU capacity/spend as the
remaining live gate without fabricated output.

## T-021 — Turn the platform tabs into a sticky scrolling story

Completed: 2026-07-21

Description: Replace the platform section's single swapping proof with a desktop sticky index and a
right-hand sequence of complete workload stories that advance through normal page scrolling.

Result: The desktop section now keeps its heading and four-item index pinned while four complete,
closely stacked workload stories scroll beside it. The active index follows the reading position,
and mouse or keyboard activation scrolls to a story without remounting its live preview. Mobile uses
the same content in a natural single-column sequence with no sticky or nested scrolling behavior.

Evidence: Prettier, focused ESLint, TypeScript, and the production build with four-page prerender
passed. The production browser matrix passed 16 checks with one intentional mobile sticky skip
across desktop, standard and 320px mobile, landscape phone, and tablet viewports; it covers active
scroll tracking, sticky release, keyboard focus, reduced motion, preview lifecycle, centered
containment, touch geometry, and horizontal overflow. No commit was created.

## T-020 — Restore standard marketing-section backgrounds

Completed: 2026-07-21

Description: Evaluate partial drafting fields across the landing page, then restore the standard
section backgrounds after the visual direction proved too diagrammatic.

Result: The experimental edge grids, tint planes, capacity bands, and route lines were removed.
Platform, typed-client, compute, examples, and final CTA sections use their established solid or
existing hero/CTA grid backgrounds again. The earlier responsive layout, centered shells, wrapped
code, and condensed typed-client proof remain unchanged.

Evidence: Source diff review confirms only the uncommitted background experiment was removed.

## T-016 — Make marketing pages multi-scale and mobile-compatible

Completed: 2026-07-21

Description: Make the public homepage and use-cases route compose intentionally from 320px phones
through landscape phones, tablets, laptops, and wide desktop while preserving the Field Manual
visual thesis and production marketing contracts.

Result: Marketing shells have safe-area-aware centered gutters, responsive rhythm, 44px touch
geometry, and accessible mobile navigation. The mobile hero exposes all six examples in a stable
3×2 control. Code examples, deployment commands, the generated-client editor, and sandbox terminal
wrap without horizontal scrolling. The typed-client Define proof is reduced from 31 to 16 lines,
retaining only its response model, endpoint, and task queue; repeated notes were folded into one
sentence, and phone typography, phase headers, gaps, and section padding are compacted.

Evidence: Formatting, scoped ESLint, TypeScript, all 261 unit tests, production build/prerender, and
diff hygiene passed. The established public-marketing suite passed 27 checks with one intentional
mobile sticky-scroll skip, its motion/viewport matrix passed nine checks, and exact shell centering
was measured through 1600px. Five production viewport projects select every hero example and audit
all code/terminal owners with no horizontal overflow. At 320px, the condensed Define proof is 16
lines in a 280px body; a production screenshot passed visual inspection and the final five-project
browser matrix passed. The independent shared bundle blocker remains with T-008.

## T-008 — Finish focused settings controllers and E2E typing — F-28

Completed: 2026-07-21

Description: Replace the mixed settings-route owner with focused workspace identity, token, and
shared deletion state machines; make browser contracts strict and fully typed; and close the token
issuance authority gap discovered during the work.

Result: The settings route is a 70-line composition root, down from 773 lines. Identity, tokens,
AWS connection, compute policy, and workspace deletion each have one controller and exact cache
owner. One-time token plaintext never enters TanStack state; all non-workspace credentials are
read-only in the dashboard; workspace writers cannot mint privileged token kinds or mutate their
authenticating credential. A single deletion provider serves Settings and the workspace switcher,
removes only canonical workspace-root queries, preserves sibling/global/current state, and exposes
a retryable recovery route for durable `deleting` workspaces. The admin-only directory projection
includes Active and Deleting workspaces but excludes Deleted tombstones. Workspace and token Zod
contracts are complete and strict, and the normal TypeScript project now includes every E2E spec.

Evidence: Web lint, full E2E-inclusive TypeScript, 261 unit tests, production build, full
BasedPyright, and lock validation pass. Four strict production-browser cases pass across Chromium
and mobile, with two intentional desktop-only identity/recovery variants; they prove exact queries,
bodies, rename routing, raw-secret acknowledgement, managed-token protection, action retry,
desktop/mobile deletion containment, surviving-session navigation, and interrupted-delete resume.
Fourteen focused API/service tests pass for token authority, self-mutation rejection, and Active /
Deleting / Deleted directory projection. Remeasurement finds no mutation/cache ownership in the
settings composition route and no workspace-scoped query key outside the canonical root. A further
61-case production Chromium regression batch passes across the authenticated shell, compute,
storage, tasks, usage, onboarding, and app lifecycle. The known independent bundle gate remains at
684.8 KiB against 675.0 KiB and was not raised.

## T-015 — Share the marketing Get started action

Completed: 2026-07-21

Description: Make the landing-page hero Get started action the single shared marketing component
used by both the hero and the marketing header while preserving the hero's visual and interaction
treatment.

Result: `GetStartedLink` now owns the label, `/dashboard` destination, branded stamp, editorial
size, arrow, focus behavior, and hover treatment. Both the hero and marketing header render that
component directly with no per-location visual override.

Evidence: Web lint, TypeScript, all 243 unit tests, and the production build passed. The focused
production marketing suite passed 25 desktop/mobile browser checks with one existing mobile sticky-
scroll skip, including accessibility, responsive containment, header interaction, and dashboard
navigation. The repository-wide bundle gate remains independently blocked at 677.0 KiB against its
675.0 KiB ceiling; this refactor added no dependency or styling.

## T-000 — Adopt the three-file work board and proportional validation

Completed: 2026-07-20

Description: Replace the oversized mixed ledger with a product-board workflow and align validation
with the predeployment/latest-only product phase.

Result: `board/todo.md` now queues defined work, `board/loop.md` contains active owned work, and
`board/finished.md` records completed outcomes. Root and owner guidance now defines safe
predeployment database reset, iteration/checkpoint/release validation tiers, scoped live smokes,
cheapest-authoritative-layer tests, infrastructure stop rules, and conditional reference audits.

Evidence: every unchecked finding and current production blocker has one board task; active dirty
database/sandbox and web scopes remain in progress; completed finding IDs are covered below.

## Accepted platform work

### Trusted User-Code Execution — F-01, F-02, F-18 task surfaces

Description: Remove service-process tenant execution and generic cron/worker paths.

Result: Tenant code traverses scheduler, container worker, OCI runtime, and runner. Raw task
creation/rerun, generic handler/command cron, and the generic worker were removed.

Evidence: the public trusted-execution workflow passed with exact PostgreSQL, Redis, object-store,
filesystem, OCI, and workspace cleanup.

### Atomic Authorization Claims — F-04

Description: Make administrator bootstrap and device claims PostgreSQL-atomic and fail-closed.

Result: Concurrent claims produce one credential; bootstrap/recovery is offline, audited, and does
not reopen anonymous authorization.

Evidence: real PostgreSQL concurrency and public device/recovery workflows passed without residual
schemas or credentials.

### WebSocket Credential Tickets — F-05

Description: Remove long-lived bearer credentials from browser WebSocket URLs.

Result: Browser shells use short-lived, audience-bound, single-use Redis tickets while CLI clients
retain header authorization.

Evidence: live shell acceptance passed replay, expiry, revocation, audience, connection, and cleanup.

### Canonical Resource Ownership And Routes — F-06, F-07, F-10, F-21, F-22, F-27

Description: Establish one mutation owner, API route, application graph, and CLI transport per
resource.

Result: `AppService` owns lifecycle, `/api/v1` owns resource CRUD, workspace lookup is read-only,
the API graph is lifespan-built, and ordinary admin commands use authenticated HTTP.

Evidence: public Compose application, workspace, pool, worker, and CLI workflows passed with exact
lifecycle and cleanup evidence.

### Acyclic Execution Architecture — F-08, F-11, F-17, F-18

Description: Remove the scheduler/execution cycle, direct worker mode, and fail-only plumbing.

Result: Workers are repository-client-only; scheduler and execution are independent and join only
at the scheduler application composition root.

Evidence: current function, queue, endpoint, and pod workflows passed; dependency-cycle and
forbidden-worker-dependency guards are green.

### Production Redis And Cache Truth — F-09, F-12

Description: Require production Redis atomicity and retain only the connected whole-object cache.

Result: Fake Redis fallbacks and the unused CacheFS/raw/gRPC architecture were removed.

Evidence: cache population, restart, corruption, repair, fallback, and exact cleanup passed through
the production cache workflow.

### Provider Lifecycle Acceptance Truth — F-03

Description: Prevent local or in-memory checks from claiming a real provider lifecycle.

Result: The fake generic provider was removed. Only the real AWS lifecycle may report acceptance;
missing authorization reports a blocker. F-15 remains queued as T-005.

Evidence: provider contract gates pass and the uncredentialed production command exits blocked.

### Helm-Owned Kubernetes Deployment — F-13

Description: Remove the second, test-only Kubernetes manifest generator.

Result: Helm exclusively owns platform resources; the retained Kubernetes package may only scale
the exact Helm-owned worker Deployment.

Evidence: render/install/upgrade/uninstall and k3d cleanup evidence passed.

### Canonical Registry Credential Parsing — F-14

Description: Establish one security-sensitive registry normalization owner.

Result: Shared credential parsing owns host, ECR, and generated-name normalization; duplicate
parsers and credential persistence paths were removed.

Evidence: cross-owner credential, collision, and secret-redaction contracts pass.

### Canonical SDK HTTP Transport — F-19

Description: Replace three divergent SDK HTTP implementations.

Result: Generated and decorated endpoint callers share one response, TLS, redirect, URL, body, and
error implementation.

Evidence: all-status, malformed-response, redirect, timeout, TLS, and connection-failure cases pass.

### Retire `shared.models` — F-23

Description: Move contracts into focused latest-only owners without aliases or facades.

Result: Legacy modules and callers were removed and ownership guards prevent resurrection.

Evidence: repository imports, contract round trips, public CLI/SDK paths, and the integrated suite
passed.

### Enforce Repository Typing Policy — F-24

Description: Replace suppressions and broad internal boundaries with truthful contracts.

Result: Standard mode and unknown diagnostics are enabled repository-wide with no ignores,
checker-only edges, or exclusions.

Evidence: full BasedPyright and the integrated Python suite passed.

### Canonical Worker Configuration — F-25

Description: Replace flattened aliases and behavior overlays with one strict nested model.

Result: Direct/auto settings, behavior aliases, and dead capacity knobs were removed; runtime
overlays retain only placement, identity, paths, and secret references. F-20 remains active.

Evidence: Compose, Helm, agent, and worker configuration parity passed.

### Pydantic, Zod, And SDK Contract Parity — F-26

Description: Verify hand-authored contracts across Python, web, and SDK boundaries.

Result: One deterministic corpus covers valid, invalid, default, null, unknown, and enum cases.

Evidence: exporter drift, Zod normalization, SDK transport, web, and browser checks passed.

### Event-Driven Startup Architecture

Description: Establish fast scheduling feedback without coupling liveness to long polling.

Result: Scheduler wakeups are event-driven, workers block for immediate pickup, heartbeat remains
independent, and runner admission preserves the one-shot function contract.

Evidence: the accepted startup-latency workflow is preserved in revision `a147028`.

## Accepted marketing work

### Three Public Marketing Directions

Description: Build and compare three complete public marketing route families.

Result: Command Surface was selected as the canonical direction.

Evidence: twelve-route desktop/mobile browser acceptance and visual review passed.

### Canonical AI Infrastructure Marketing Experience

Description: Promote the selected family and remove prototype routes.

Result: Canonical public routes retained the authenticated dashboard handoff; later positioning
tasks refined the copy without reintroducing prototypes.

Evidence: route, auth-boundary, accessibility, and browser acceptance passed.

### Canonical Marketing Copy Compression

Description: Shorten public pages without losing workload or operational truth.

Result: Scan-level copy became direct and compact while route isolation and capability coverage
remained intact.

Evidence: copy, metadata, accessibility, and production-browser gates passed.

### Full-Application Platform Positioning

Description: Present LazyCloud as a broad Python application platform.

Result: The route family established the application-platform story, later sharpened by the
serverless AI positioning task.

Evidence: page-heading, metadata, and browser checks passed.

### Serverless AI Cloud Positioning

Description: Make inference, function graphs, background work, sandboxes, and connected compute the
truthful content spine.

Result: Public messaging matches implemented SDK and production workload capabilities.

Evidence: route-family content, metadata, accessibility, and browser checks passed.

### Beam-Pattern Marketing Route Family

Description: Establish one shared route grammar and production-derived proof structure.

Result: Four public routes use the common component system; the visual skin was later superseded by
the accepted Field Manual design.

Evidence: navigation, interaction, reduced-motion, budget, and browser gates passed.

### Field Manual Marketing Skin

Description: Apply the selected paper, ink, and LazyCloud-blue visual system to the route family.

Result: Paper surfaces and dark proof sheets replaced the graphite/comet treatment while retaining
capability and route behavior.

Evidence: contrast, lint, typecheck, unit, build, bundle, and desktop/mobile browser gates passed.

## T-002 — Finish the unified marketing and dashboard design batch

Completed: 2026-07-20

Description: Finish the shared design system and homepage previews while keeping public landing
routes permanently light, examples geometrically stable, and authenticated surfaces on the same
token and primitive system.

Result: Endpoint and queue previews now present readable, deterministic live telemetry; the queue
opens on a populated frame and advances within 450 ms. The training graph completes in 8.4 seconds
instead of 13.2 seconds, sandbox typing is 2.25 times faster, first process feedback arrives in 2.24
seconds, and termination appears in 13.12 seconds. Tailwind utility generation has one owner and the
marketing stylesheet retains only route-specific art direction. The cohesive web batch is committed
as `6965175d`.

Evidence: ESLint, TypeScript, all 41 Vitest files / 229 tests, production build and prerender,
bundle budgets, accessibility, and desktop/mobile Playwright acceptance passed. Client gzip remains
within the unchanged 675.0 KiB ceiling and the examples retain stable desktop/mobile geometry.

## T-010 — Simplify landing telemetry comparisons

Completed: 2026-07-20

Description: Make the task-queue, training, and inference previews communicate changing operational
load without empty initial states, misleading series, or cramped mobile geometry.

Result: Queue telemetry opens on populated history and immediately animates queued tasks,
processing tasks, and online workers together on one time scale. Training + ETL now presents a
production run summary, three execution-derived KPIs, and a dependency-aware task timeline; its
bars progress at three times their prior rate and reset on reactivation. Inference traffic returns to the
original cumulative step-area concept with blue, amber, and green route bands for `/generate`,
`/embed`, and `/classify`, strengthened by aggregate in-flight, utilization, and container metrics,
an uncluttered plot canvas, and compact route summaries. Populated deterministic history advances
every 650 ms with bounded irregular movement and resets when its story becomes inactive. The pinned
platform chapter now centers a fixed-height story stage between equal top and bottom whitespace and
uses 220dvh rather than 270dvh, reducing the oversized trailing scroll range.

Evidence: ESLint, TypeScript, production build and prerender, and six focused desktop/mobile
production-browser checks passed. The browser checks prove populated queue startup, queue
progression and reset, exactly three queue lines, accelerated training progression and reset, three
distinct inference area and line series, no labels over the plotted data, prominent contained route
metrics, dynamic path progression, reset on reactivation, and paper-light surfaces. Desktop and mobile production screenshots passed visual
inspection. The production geometry check additionally proves a 2.1–2.3 viewport chapter, equal
stage whitespace, a stable pinned heading across story changes, and equal story ranges. The
repository-wide bundle gate is currently blocked by concurrent authenticated-app
growth: total client gzip is 677.0 KiB against the unchanged 675.0 KiB ceiling; these marketing
chart refinements add only a small fraction of that total, so the budget was not raised and unrelated
work was preserved.

### T-001 accepted checkpoint — Current schema and automatic Compose bootstrap

Description: Replace undeployed database transition machinery with one current baseline, retain
container-pinned sandbox routing, and make a fresh production-shaped local stack start without a
manual credential pre-step.

Result: Alembic now has the sole `20260720_current_schema` baseline and refuses stale/nonempty
schemas. Historical migrations, upgrade/downgrade, backup/restore, compatibility smokes, and dead
dependencies are removed. Compose and Helm call the same database initializer. Compose then creates
one idempotent offline administrator credential in a dedicated mode-`0600` volume before creating
worker credentials. Public login accepts `--token`, then `LAZYCLOUD_TOKEN`, then the stored profile;
output remains redacted. BuildKit caches are LazyCloud-namespaced.

Evidence: focused current PostgreSQL suites passed 38 tests, Compose/Helm configuration passed 25
tests, proxy/sandbox tests passed 28 tests, all 1,191 Python files pass Ruff/format, BasedPyright has
zero issues, and the integrated run passed 2,273 tests before exposing three obsolete assertions
that now pass focused. A full image build and exact fresh root Compose startup passed; all services
became healthy, bootstrap jobs exited zero, credential mode/redaction/idempotence and saved-profile
login were proven, and Helm lint/render passed. T-001 remains queued only for the T-003-dependent
workspace cleanup proof.

## T-006 — Relational token truth and exactly-once single-use authorization — F-20

Completed: 2026-07-20

Description: Make PostgreSQL token columns the sole durable truth and turn the public non-reusable
token option into an atomic exactly-once authorization contract.

Result: Tokens now use an explicit relational mapper and focused repository operations with no JSON
shadow. Non-reusable tokens are never positive-cached; their first valid request atomically commits
a terminal consumed/revoked state, and concurrent or later requests fail. PostgreSQL constraints
reject non-reusable or nonterminal consumption states, and expired or consumed tokens cannot be
reactivated. The current baseline is `20260721_token_terminal`. The cohesive slice is committed as
`1b54e394`.

Evidence: An eight-way PostgreSQL race produced exactly one authenticated claimant and seven
failures. Live public API acceptance produced one success then HTTP 401, confirmed the terminal row
directly in PostgreSQL, deleted the credential, and proved exact API/operator/database token parity
plus scoped Redis cleanup. All 2,281 repository tests passed with 197 expected skips; Ruff and format
passed 1,193 files and BasedPyright reported zero issues.

## T-011 — Make device authorization relational and collision-safe — F-20

Completed: 2026-07-20

Description: Remove device-authorization JSON shadow state while preserving the public device login
workflow, then make readable user-code allocation rely on PostgreSQL uniqueness rather than a
lookup-before-insert race.

Result: `device_authorizations` now has one relational truth, a canonical identity record, and a
direct row mapper. Database constraints enforce status, workspace binding, and terminal consumption.
Pending creation uses bounded savepoint retries on unique collisions; approve/deny/claim remain
guarded transitions, and approved consumption plus token issuance remain one transaction. The sole
baseline is `20260721_device_relational`. The cohesive slice is committed as `77bff94b`.

Evidence: Fresh PostgreSQL repository/schema checks passed nine tests, and the eight-way concurrent
decision/claim proof retained exactly one winner at each transition. Fresh root Compose startup was
healthy after deleting only 15 revalidated `com.docker.compose.project=lazycloud` volumes. Live
atomic-authorization acceptance passed public HTTP and real CLI login, retained two consumed audit
rows, left zero pending codes and scoped Redis keys, and restored token inventory to its exact
three-row baseline. Focused manager rerun passed 29 tests with clean Ruff and format checks.

### T-008 accepted checkpoint — AWS lifecycle controller and strict API fixture

Description: Separate AWS connection lifecycle ownership from presentation and replace permissive
AWS onboarding mocks with exact, Zod-validated API contracts.

Result: One controller now owns all six mutations, popup handoff, concurrent-action exclusion,
visible retryable errors, authoritative cache replacement, and removal invalidation. The browser
fixture validates method/path/query, request and response schemas, JSON/204/SSE behavior, explicit
replacement, and fails unmatched, duplicate, wrong-method, or malformed traffic. All 17 onboarding
routes use it. The checkpoint is committed as `d5c82e94`.

Evidence: Web lint and TypeScript passed; 38 controller/lifecycle tests, 20 desktop/mobile fixture
and onboarding checks, 12 production-mode onboarding checks, and the production build passed.
F-28 remains queued for compute-policy, workspace-identity, token, deletion, and full E2E typing.

### T-008 accepted checkpoint — Conflict-safe compute-policy controller

Description: Give compute-policy editing one owner so background refreshes and concurrent writers
cannot silently overwrite each other while preserving the user's field-level draft.

Result: A focused controller now owns the authoritative query, base revision, ordered-field draft,
dirty/review/error state and one save. Clean refreshes replace exactly; dirty refreshes and HTTP 409
field-rebase local edits over the newest authority and require explicit review. Saves use the frozen
revision, never fabricate optimistic server state, and replace cache/draft only with the exact server
response. The update transport now accepts a precise Zod-inferred request. The checkpoint is
committed as `5f5cccf0`.

Evidence: Focused controller/schema/query tests passed 21 cases; the full web suite passed 243.
ESLint, TypeScript, production build, and 16 desktop/mobile checks in both development and production
browser modes passed, including exact revisioned save, conflict GET/rebase/review/retry, pending and
saved states, and horizontal containment. F-28 remains queued for workspace identity, token,
deletion, and repository-wide E2E typing.
