# Web packet

Parity sections from tasks/parity.md: "Dashboard", plus every dashboard item
in the other sections. The dashboard in `web/` runs against the public API in
`contracts/openapi.yaml`.

## Constraint

The dashboard's styling, layout, components, routes and copy are finished
work. Every file under `web/src` outside the data layer matches
origin/go-rewrite. The one component edit is TaskDrawer's cancel button,
which now calls `cancelTask` from the query layer. Data shapes change only in
`web/src/lib/api` and `web/src/lib/queries`.

## Decisions

- openapi-typescript generates types from `contracts/openapi.yaml`
  (`bun run apigen`). The output in `web/src/lib/api/generated` is checked in,
  and CI fails if it drifts. Requests go through openapi-fetch. `ok()` returns
  the success body or throws `ApiError`.
- Components keep the reference view types (the zod-inferred types in
  `lib/api/schemas`). The query layer maps API resources into them in
  `lib/api/views.ts` and parses nothing at runtime. A field the API lacks
  gets the empty value the component already renders for absence.
- The session is the HttpOnly `__Host-lazycloud_session` cookie. The sign-in
  link returns to `/callback#code=<destination>`. The callback page reads
  `GET /v1/me` and stores a marker in localStorage so the reference AuthGate
  works unchanged. A 401 clears the marker. A browser that already holds a
  session and starts sign-in goes straight to `return_to`.
- Query keys stay keyed by workspace ID, and `workspaceName(id)` resolves the
  path name from the session read. Routes keep app IDs. `lib/queries/directory.ts`
  maps app and workload IDs to names with one cached read each.
- A stub is `app:function:release`, and a deployment row is
  `workload:version`. Task statuses map to the reference set: queued becomes
  pending, or retry once an attempt has failed; succeeded becomes complete;
  a timeout failure becomes timeout.
- Log views read the NDJSON log streams. `lib/api/sse.ts` translates the
  reference log and change stream URLs, so `useEventStream` and
  WorkspaceLiveUpdates are unchanged.

## Progress

- [x] Foundation: sign-in, sign-out, `/activate`, invitations, tokens,
  workspaces, members
- [x] Apps, workloads, versions, schedules, tasks, task drawer, logs,
  containers
- [x] Storage: secrets, volumes, disks, artifacts, queues, maps
- [x] Observability: activity, task metrics, latency, container metrics,
  call graph, lifecycle, account metrics, live changes
- [x] Admin users (operations), compute, machines, AWS connection, fleet
- [ ] Billing, usage, pricing, complimentary grants: ready on web-billing and
  web-admin-users, waiting for #424
- [ ] Invoke URLs, endpoint kinds, domains: ready on web-endpoints, waiting
  for the endpoints packet
- [x] Stack journeys in `web/tests/e2e/stack.spec.ts` against a private stack

## Intentional differences from the reference

- `/callback` receives no code. The server's GitHub callback sets the
  cookie, and the page confirms it with `/v1/me`.
- Listed tokens show no prefix. The API stores only digests.
- Log history has no backward paging.
- Deleting a workload version deletes the workload, so only a workload's
  active row or its last version offers Delete.
- Map keys need at least one character.

## Gaps

Data the API does not provide yet. Components show their empty state or the
server's error.

- Pods, devboxes, sandboxes and shells have no API. Their panels show the
  server's "no such operation".
- Billing, usage and pricing until #424. The Compute "Add cloud" button and
  its upgrade gate read the billing summary. Admin users show "No plan" and
  $0.00 usage.
- Invoke URLs and the HTTP invoke until the endpoints packet. The function
  playground submits a task instead.
- Containers have no image, command, ports or exit code. Tasks carry no
  handler, args or kwargs. Results have no rich display.
- The artifact summary has no cost fields. Its tooltip reads "$0.00 accrued",
  which is not true. Fixing that needs the cost data or a component change.
- Volumes have no update time, queues have no write rate, and disks have no
  workload reference.
- `contracts/http_contract_cases.json` stays because a Python test reads it.
- `/pricing` reads `/api/v1/pricing` until billing serves pricing.

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

- `bun run typecheck`, `lint`, `format:check`, `build` and vitest
  (126 tests) pass. The mocked smoke, onboarding and marketing specs pass
  on both projects apart from the two fixme tests.
- The stack journeys pass on chromium and mobile against server, scheduler
  and agent on private ports. They cover sign-in, workspace create, rename,
  invite and delete, deploy with the SDK, playground invoke, task drawer
  logs and container, tokens, `/activate`, secrets, volume upload and
  download, queues, maps, task artifacts, and accepting an invitation.
- Before and after screenshots of Apps, the app, the workload, Tasks, the
  task drawer, Secrets, Tokens and sign-in match in layout and copy. Only
  the data differs.
