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
   `stopping`, `stopped` (plain stop), `hibernate_unverified` (hibernated,
   evidence unknown or failed), `image_saved` (hibernated, evidence saved),
   `starting` for `resuming`. `ready` is serving on the target agent, or for
   a reserve, prepared for the target agent. Keep the cursor and limit.
3. API mapping in `fleet.go`; drop the zeros at lines 54-55.
4. Check the page on a local stack with browser automation: both tabs, an
   expanded market, a stopped and a hibernated node, the expired message.

## Progress

## Intentional differences

## Evidence

## Gaps and unverified boundaries

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
