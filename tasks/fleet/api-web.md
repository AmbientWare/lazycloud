# Fleet API and dashboard

## Scope

The admin Fleet view's data: reserve numbers per market and reserve states
per node, read from the published plan and host phases. The page keeps its
look.

Parity sections (tasks/fleet/parity.md): Admin API and dashboard.

Owns:

- `internal/compute/fleet_admin.go` and `queries/fleet_admin.sql`.
- `internal/api/fleet.go`.
- `contracts/openapi.yaml`: descriptions of `FleetState`, `FleetMarket`
  `reserve_ready` and `reserve_target` only; operations, fields and enums
  stay as they are. Regenerated Go, TypeScript and Python bindings.
- `web/src/components/shared/SettingsDialog/AdminSettings/FleetSettings.tsx`
  and its tests, only where the data needs it.

Migration: none. Protobuf: none.

Depends on: the planner packet (`fleet_markets`) and the provider packet
(phases). Can start against plan.md's schema while the planner packet is in
review.

## Plan

1. `Fleet`: read `fleet_markets` for each market's published plan
   (`generated_at`, `expires_at`, targets, measures, reason) and count
   hosts by state from one host query. With no row or an expired row, return
   no plan, so the dashboard shows its "Capacity data unavailable" or
   "expired" message, as the reference did (FleetSettings.tsx:111-123).
2. `FleetNodes`: list platform hosts with an instance id, so refused launches
   never show; map phases and reserve facts to states: `preparing`,
   `stopping`, `stopped` (plain stop, or a hibernation whose evidence failed
   or is unavailable), `hibernate_unverified` (hibernated, evidence
   unknown), `image_saved` (hibernated, evidence saved),
   `starting` for `resuming`. `ready` is serving on the target agent, or for
   a reserve, prepared for the target agent. Keep the cursor and limit.
3. API mapping in `fleet.go`; drop the zeros at lines 54-55.
4. Check the page on a local stack with browser automation: both tabs, an
   expanded market, a stopped and a hibernated node, the expired message.

## Progress

Branch `fleet-api-web` from `origin/fleet-planner`, rebased onto
`d8af821fa34cdca94e7a5028a9062f09cbfa8fd5` (the planner's review fixes).
All four plan steps are done.

- `Fleet` returns the newest reserve pass's markets from `fleet_markets`
  (`PublishedPlan`) while they are current, CPU markets first; no row or an
  expired one is no plan. Reserve ready and target are the plan's
  `reserve_ready` and `stopped_target`; allocated is its load; states and
  reason are the published ones. The zeros in fleet.go are gone, and so are
  `compute.FleetMarket`, the 10,000-host scan and the network reason.
- The rollout is one grouped SQL count (`FleetRollout`).
- `FleetNodes` lists platform hosts with an instance id; the state is the
  planner's `fleetStateOf`, and `Ready` is computed in compute with
  `onRelease`.
- `Propose:` commits: `fleetStateOf` takes a `hostStanding` and
  `preparedFor` became `onRelease(host, version, release)` in
  planner_snapshot.go, so Nodes and the planner share one mapping; the
  OpenAPI descriptions of `warm_target`, `FleetNode.ready` and
  `FleetSummary.plan`; migration 0006, index `hosts_platform` on platform
  hosts that still exist, so Nodes and the rollout skip deleted history
  (`TestFleetAdminReadsStayFlatAsDeletedHostsGrow`: 42 buffers each with
  1,000 or 20,000 deleted hosts; 646 and 686 without the index).
- The rollout's connected rule matches `fleetStateOf`, including an emptied
  consolidating host
  (`TestFleetRolloutAndNodesAgreeOnAnEmptiedConsolidatingHost`).
- FleetSettings.tsx needed no change.

Parity lines (internal/api, web):

- `GET /v1/fleet`: `TestFleetReturnsThePublishedPlanUntilItExpires` (runs
  the real pass, checks every number against the published plan, the
  hibernated host as reserve ready, the plan omitted before publication and
  after expiry).
- `GET /v1/fleet/nodes` reserve states and readiness, refused launch absent,
  paging, rollout: `TestFleetNodesListReserveStatesAndNeverARefusedLaunch`.
- Administrators only: `TestFleetIsForAdministratorsOnly` (its host now has
  an instance).
- Dashboard: FleetSettings.test.tsx (published targets and expanded states,
  unavailable, expired, reserve state labels on Nodes).

## Intentional differences

- An expired plan is omitted, as the reference's `published()` did; the
  "expired" message shows when a loaded plan lapses on an open page,
  "unavailable" after a refresh.
- The reference also hid a plan published for another agent release. The
  published plan carries no release; its `reserve_ready` counts reserves
  prepared for the target at the pass, at most 60 s old.
- A host bought for the reserve, or a reserve refreshing, shows "Preparing
  reserve" while it starts (the planner's mapping, so Nodes and Capacity
  agree); the reference showed Starting. A resume to serve shows Starting.
- A requested host not launched yet has no instance and is not on Nodes,
  though the plan counts it as starting.
- `ready` follows the rollout: a host outside a partial rollout is ready on
  its own release, as `AnswerReserve` decides.
- Rollout phases count stopped, stopping and preparing hosts as `reserve`,
  not `offline`; `complete` still means no connected host on another
  release.

## Evidence

- `go test -race ./internal/api ./internal/compute` pass; `./check.sh`
  passes; `bun run test` for AdminSettings passes (6); `bun run apigen`,
  `go generate` and the api datamodel-codegen profile leave the tree clean.
- `acceptance/neki/check.sh`: 682 checked, 0 router failures.
- Screenshots (Playwright, own stack: Compose project `lcapiweb`, Postgres
  27432, server 29080-29083, dashboard 29173, state in `.lazycloud/apiweb`,
  taken down with its volume): /tmp/lazycloud-api-web-shots/
  fleet-capacity.png, fleet-capacity-expanded.png, fleet-nodes.png,
  fleet-expired.png, fleet-unavailable.png. The stack had no planner, so
  the plan rows were seeded in SQL; the API tests cover the real pass.

## Gaps and unverified boundaries

- The page against a planner-published plan on real hosts runs on prod in
  the acceptance packet.

## Verification

- API tests with real PostgreSQL: administrators only
  (`TestFleetIsForAdministratorsOnly` stays), published numbers returned,
  expired plan omitted, reserve states listed, a refused launch absent.
- Web: bun tests for FleetSettings with Node 22; `bun run apigen` leaves the
  tree clean; browser automation screenshot of each state on your own local
  stack copy (own ports, compose project name and state directory), taken
  down afterwards.
- `go test -race ./internal/api ./internal/compute`, `./check.sh`.
- Real EC2: none needed in `default-test`; the acceptance packet checks the
  page on prod after the Ship.

## Brief

```text
You own the api-web packet of the fleet capacity work for LazyCloud (repo
github.com/AmbientWare/lazycloud). Work alone; do not start sub-agents.
Create branch `fleet-api-web` from origin/fleet-capacity-plan (after the
planner packet merges, or from origin/fleet-planner if the integrator says
so) and record the SHA in tasks/fleet/api-web.md.

Read first: AGENTS.md, tasks/fleet/README.md, tasks/fleet/plan.md ("Nodes
and Capacity API"), tasks/fleet/parity.md, tasks/fleet/api-web.md. The
reference is commit 9e259ce75: read it with `git show 9e259ce75:<path>` for
behavior only; never read its env or credentials. Start with
packages/compute/src/compute/fleet_status.py and
apps/web/src/components/shared/SettingsDialog/AdminSettings/FleetSettings.tsx.

Goal: deliver the parity lines marked "Packet: api-web" one to one. Same
pages, layout and copy; internals on the new data with no adapter layer.
Record differences in tasks/fleet/api-web.md.

You own the files under "Owns" in tasks/fleet/api-web.md. Stay off the
planner, provider, policy and host protocol files, migrations/ and every
other OpenAPI schema. A change elsewhere is a `Propose: ...` commit,
explained in your report.

Environment: go1.27.1 (export GOTOOLCHAIN=go1.27.1 if needed); bun and Node
22 for web; uv from the repo root for Python bindings. Test PostgreSQL:
`docker compose -f compose.test.yaml up -d --wait`; shared, never stop it.
Run a local stack only as your own copy and take it down afterwards.

Verify: the checks under Verification in tasks/fleet/api-web.md.

Commit and push after each meaningful step, one-line subjects, no trailers.
Don't open PRs. Report in under 400 words: parity lines delivered with test
names, screenshots taken, proposed shared changes, gaps.
```
