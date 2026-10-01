# Web packet

Parity sections from tasks/parity.md: "Dashboard", plus every dashboard item
in the other sections. The dashboard in `web/` runs against the public API in
`contracts/openapi.yaml`.

## Decisions

- Types come from `contracts/openapi.yaml` through openapi-typescript
  (`bun run apigen`, output in `web/src/lib/api/generated`, checked in and
  verified by CI). Requests go through openapi-fetch, typed by path and
  method; `ok()` returns the success body or throws an `ApiError` carrying
  the status and the typed error code. The server validates every request
  and response against the same document, so the hand-written Zod schemas
  of an area are deleted when it moves to the public API.
- The browser session is the HttpOnly `__Host-lazycloud_session` cookie from
  `/auth/github/start`. Requests are same-origin; the browser's own Origin
  header passes the server's check on cookie-authenticated mutations. The
  page holds no credential. `GET /v1/me` answers who is signed in; a 401 from
  any query re-reads it so the auth gate shows the sign-in screen.
- Query keys are scoped by workspace name, the identifier every path uses.
  A rename moves the dashboard to the new name's keys.
- Collections page with `limit`, `cursor` and `next_cursor` through one
  helper (`selectPages`, `nextPageCursor`).

## Plan

1. Foundation: generated types, client, cookie session, sign-in, sign-out,
   `/activate`, invitations, tokens, workspaces, members.
2. Apps, workloads, versions, schedules, tasks, task drawer, logs and
   containers on the control and workload-runtime APIs.
3. Storage tabs: secrets now; volumes, disks, artifacts, queues and maps as
   the storage packet merges.
4. Areas whose packets are still running (endpoints, compute, billing,
   observability, workloads): wire them as they merge, otherwise list the
   exact gap.
5. Playwright journeys against a private local stack.

## Progress

- [x] Foundation
- [ ] Apps, workloads, tasks, logs, containers
- [ ] Storage
- [ ] Other areas as they merge
- [ ] Local stack journeys

## Intentional differences from the reference

- `/callback` is gone: the server's GitHub callback sets the session cookie
  and redirects (see tasks/identity.md).
- Token rows no longer show a token prefix; the API stores only digests and
  returns none. The freshly issued value is masked from its own first
  characters.
