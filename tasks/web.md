# Web packet

Parity sections from tasks/parity.md: "Dashboard", plus every dashboard item
in the other sections. The dashboard in `web/` runs against the public API in
`contracts/openapi.yaml`.

## Rewrite rules

The dashboard looks and behaves as the reference does: same pages, layout,
styling, copy and route shapes. Its internals are rewritten for the public
API. The before and after screenshots of every main page are the guard for
visual parity.

- Components and hooks consume the generated types directly:
  `Schemas["X"]` from `@/lib/api/client`, calls through `api` and `ok()`.
  No hand-written zod schemas for API resources, no view types that map new
  shapes back into old ones, no compatibility mappings and no old endpoint
  helpers. `lib/api/schemas`, `lib/api/views.ts`, `lib/api/workspaces.ts`,
  `lib/api/sse.ts` translation and `lib/queries/directory.ts` go away.
- The workspace is addressed by name. `useWorkspace().workspace` is
  `Schemas["Workspace"]`; query functions take the workspace name, and
  `workspaceQueryKeys` are keyed by it. A rename moves to the new keys.
- Apps are addressed by name. The route param is `$app`:
  `/w/$workspace/apps/$app/workloads/$kind/$name`.
- Status words are the API's: queued, running, succeeded, failed,
  cancelled. A timeout shows through the task's failure.
- Live data comes from the change stream (`/v1/workspaces/{name}/changes/stream`)
  through WorkspaceLiveUpdates, not polling, where the stream has a topic for
  it. Log views read the NDJSON log streams directly.
- One API response that answers a page replaces client-side aggregation of
  several calls. Filtering happens on the server; a missing filter is an API
  change, never a filter over one client page.
- Restructure a component or hook where its old structure existed only for
  the old backend. Keep its rendered markup.
- Tests prove behavior through the new shapes. Tests that only covered old
  shapes go.
- The one allowed UI change is removing an element whose data the API does
  not keep and the product does not need: the volume "updated" time and the
  queue write rate are gone.

## Progress

- [x] Foundation: sign-in, sign-out, `/activate`, invitations, tokens,
  workspaces, members, search
- [x] Apps, workloads, versions, schedules, playground, tasks, task drawer,
  logs, containers, endpoint and ASGI request records, domains
- [x] Storage: secrets, volumes, disks, artifacts, queues, maps
- [x] Observability: activity, task metrics, latency, container metrics,
  call graph, lifecycle, account metrics, live changes
- [x] Admin users, billing, usage, pricing, compute, machines, AWS
  connection, fleet
- [x] Pods, devboxes, sandboxes, shells and container files on the
  workloads API, and the app card's Devbox chip
- [x] Rewrite: components read the generated types; `lib/api/schemas`,
  `views.ts`, `directory.ts`, the workspace name registry and the old
  request helpers are gone
- [x] Stack journeys in `web/tests/e2e/stack.spec.ts` against a private stack

## Intentional differences from the reference

- `/callback` receives no code. The server's GitHub callback sets the
  cookie, and the page confirms it with `/v1/me`.
- Workspace and app URLs carry names, `/w/$workspace/apps/$app`, as the API
  addresses them. The reference used the app ID.
- Status chips show the API's words: queued, running, succeeded, failed,
  cancelled. A timeout shows through the task's failure.
- Log history has no backward paging.
- Deleting a workload deletes every version of it. In the version list only
  a workload's one remaining version offers Delete.
- Map keys need at least one character.
- Endpoint and ASGI requests are the edge's request records, listed where
  the reference listed their tasks. A request is succeeded, failed for a
  5xx answer, or cancelled when the caller left first (499).
- Removed because the API keeps no such data and the product does not need
  it: the volume "updated" time, the queue write rate, the Tasks page's
  single-value Type filter, and the container tab's Worker and Working
  directory rows.
- The playground covers functions and endpoints, as the reference did; an
  ASGI app answers on its same-origin path but has no playground.

## Gaps

- Devbox root disks need `nbd-client` and root on the host; on this host a
  devbox start fails with that reason, which the page shows.
- `lazycloud devbox ... ssh` fails with "No such command 'ssh-proxy'" in the
  CLI (workloads packet).

## Pre-existing UI issues for the user to decide

Both also fail on the reference's own specs. The specs mark them
`test.fixme`, and the UI is unchanged.

- Workspace search. Type a query that filters out the selected "Apps"
  destination, and no result is selected, so Enter does nothing. cmdk 1.1.1
  with `shouldFilter=false` keeps the stale value. ArrowDown and Home
  reselect a result.
- Marketing axe check. `aria-prohibited-attr` on `<div class="run-test-checks"
  aria-label="8 tests passed">` in `RunWorkloadViews.tsx`. An aria-label
  needs a role on a div.

## Evidence

- Handwritten `web/src` (generated types and tests excluded) is 35,519
  lines against 38,799 in the reference UI it replaced; the full diff is
  +6,467 / -9,377 lines including tests.
- typecheck, lint, format:check, build and vitest (120 tests) pass. `bun run
  apigen` leaves the generated types unchanged. The mocked smoke, onboarding
  and marketing specs pass on both projects apart from the two fixme tests.
- Go: gofmt, go vet, golangci-lint and `go test -race ./cmd/... ./internal/...`
  pass. Python: ruff and pytest (379 tests) pass.
- The stack journeys pass on chromium and mobile against a server,
  scheduler and agent built from this tree on private ports: sign-in,
  workspaces, deploy with the SDK, function playground, task drawer logs
  and container, the endpoint playground, an ASGI app on its same-origin
  path, tokens, `/activate`, secrets, volumes, queues, maps, artifacts and
  invitations.
- Before and after screenshots of 20 main pages match in layout and copy.
  The differences are data, the API status words and the removed elements
  above.
