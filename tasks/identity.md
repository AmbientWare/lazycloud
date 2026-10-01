# Identity packet

Parity sections: "Auth and accounts" and "Workspaces, members and invitations"
in tasks/parity.md, plus the email outbox from "Notifications". Migration
`migrations/0002_identity.sql`. No protobuf.

## Outcome

A person signs in to the dashboard with GitHub and gets an account and an
owned workspace. `lazycloud login` with no token runs the device-code flow,
the person approves it at `/activate`, and the CLI stores an account token.
Tokens are listed, created with an expiry and revoked. Workspaces are listed,
created, renamed and deleted with recovery. Members are listed, their roles
changed, removed or they leave. Invitations are sent by email through a
durable outbox, resent, revoked, previewed, accepted and declined.

## Owners

- `internal/identity`: users, GitHub sign-in, browser sessions, device codes,
  API tokens, workspaces, members, invitations and the authorization policy.
  `Authenticate` and `AuthorizeWorkspace` keep their contract; `Principal` and
  `Workspace` gain fields.
- `internal/notifications`: the email outbox. Rows commit in the caller's
  transaction. The scheduler delivers them through Resend with an idempotency
  key, 8 attempts with capped exponential backoff, purges bodies 2 days after
  settling, and the server records delivery reports from `/webhooks/resend`.
- `internal/api`: identity and webhook handlers in new files.
- `python/lazycloud`: device-code `login`, `workspace` commands on the new
  API, the out-of-date client notice.

## Decisions

- PostgreSQL is the only authority: sign-in state is a `__Host-` cookie,
  sessions, device codes, tokens and invitations are rows. No Redis.
- The browser session is an HttpOnly, Secure, SameSite=Lax cookie naming a
  `sessions` row that lasts 12 hours. Unsafe requests authenticated by the
  cookie must carry an `Origin` equal to the public URL.
- The GitHub callback sets the session cookie and redirects to `return_to`
  or `/dashboard`. The reference redirected to `/callback#code=` and the page
  traded the code for a token it kept in JavaScript; with an HttpOnly cookie
  no token reaches the page, so the exchange step disappears.
- Device codes follow RFC 8628: user code `XXXX-XXXX` from a consonant
  alphabet, 15 minute expiry, 5 second interval, `slow_down` adds 5 seconds
  when a client polls early. The approving session's user receives a device
  token named after the client.
- Tokens keep `lc_` + SHA-256. Names, optional expiry (1 to 90 days),
  revocation and a device flag are columns. `last_used_at` is collected in
  memory and written in one batched update every 30 seconds.
- Workspace roles are owner, administrator and member. Only platform
  administrators create or delete workspaces, as in the reference. Any member
  renames. Administrators invite, change roles and remove; members leave; the
  owner is neither removed nor demoted.
- Deleting a workspace marks it `deleting` and revokes its restricted tokens
  in one transaction; every other request to it is refused. The scheduler
  then cancels its tasks through execution, waits for its containers to stop,
  deletes its objects through storage and removes the rows. Deleting again
  resumes a stuck deletion.
- Plan limits on members and workspaces belong to billing. `admitMember` and
  `admitWorkspace` in identity are the seam; they admit everything today.

## Plan and progress

- [ ] Migration 0002
- [ ] Identity: users, sign-in, sessions, tokens, device codes
- [ ] Identity: workspaces, members, invitations, deletion
- [ ] Notifications outbox, Resend sender, webhook, purge
- [ ] OpenAPI paths and handlers, cookie auth, client version header
- [ ] Scheduler loops: email delivery, housekeeping, workspace deletion
- [ ] Python: login device flow, workspace commands, version notice
- [ ] Tests, lint, measurements

## Intentional differences from the reference

- The `/callback` page exchange is gone (see Decisions).
- A first GitHub sign-in whose verified primary email matches an existing
  account without a GitHub identity links to that account, so an operator
  can bootstrap an administrator with `server admin create-user` and sign in
  as it. The reference linked only by a pre-recorded GitHub user id.
- Workspace audit history is not carried over; no parity item shows it.
