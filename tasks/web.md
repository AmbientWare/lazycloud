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
  workspaces, members
- [x] Apps, workloads, versions, schedules, tasks, task drawer, logs,
  containers
- [x] Storage: secrets, volumes, disks, artifacts, queues, maps
- [x] Observability: activity, task metrics, latency, container metrics,
  call graph, lifecycle, account metrics, live changes
- [x] Admin users (operations), compute, machines, AWS connection, fleet
- [x] Billing, usage, pricing, complimentary grants
- [x] Invoke URLs, the function playground's HTTP invoke, endpoint and ASGI
  workloads, their request records in Activity and the task drawer, domains
- [ ] Pods, devboxes, sandboxes and shells: wait for the workloads packet
- [x] Stack journeys in `web/tests/e2e/stack.spec.ts` against a private stack

## Intentional differences from the reference

- `/callback` receives no code. The server's GitHub callback sets the
  cookie, and the page confirms it with `/v1/me`.
- Listed tokens show no prefix. The API stores only digests.
- Log history has no backward paging.
- App URLs carry the app name, `/w/$workspace/apps/$app`, as the API
  does. The reference used the app ID.
- Deleting a workload deletes every version of it. In the version list only
  a workload's one remaining version offers Delete, and the app's workload
  list says the delete takes every version.
- Map keys need at least one character.
- Endpoint and ASGI requests are the edge's request records, listed where
  the reference listed their tasks. A request is complete, failed for a
  5xx answer, or cancelled when the caller left first (499). A
  workspace-wide list of them shows each app's newest page only.

## Gaps

Data the API does not provide yet. Components show their empty state or the
server's error.

- Pods, devboxes, sandboxes and shells have no API. Sandbox and shell
  panels show the server's "no such operation". The Pod instance drawer and
  the devbox panels stay in place and show "Pods and devboxes have no API
  yet" until the workloads packet serves them; the API lists no Pod or
  devbox workload, so the app and workload pages show none.
- Containers have no image, command, ports or exit code. Tasks carry no
  handler, args or kwargs. Results have no rich display.
- The artifact summary has no cost fields. Its tooltip reads "$0.00 accrued",
  which is not true. Fixing that needs the cost data or a component change.
- Volumes have no update time, queues have no write rate, and disks have no
  workload reference.
- `contracts/http_contract_cases.json` stays because a Python test reads it.

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
  (124 tests) pass. The mocked smoke, onboarding and marketing specs pass
  on both projects apart from the two fixme tests.
- The stack journeys pass on chromium and mobile against server, scheduler
  and agent on private ports. They cover sign-in, workspace create, rename,
  invite and delete, deploy with the SDK, playground invoke, task drawer
  logs and container, tokens, `/activate`, secrets, volume upload and
  download, queues, maps, task artifacts, and accepting an invitation.
- An endpoint deployed to the local stack shows its invoke URL, route and
  methods, and its request records open in the drawer. Its requests hang
  locally and end as 499 after the caller's timeout. That path belongs to
  the endpoints packet and is unverified here.
- Before and after screenshots of Apps, the app, the workload, Tasks, the
  task drawer, Secrets, Tokens and sign-in match in layout and copy. Only
  the data differs.
