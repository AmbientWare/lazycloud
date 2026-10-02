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
changed, removed or they leave. Invitations are emailed through a durable
outbox, resent, revoked, previewed, accepted and declined.

## Owners

- `internal/identity`: users, GitHub sign-in, browser sessions, device codes,
  API tokens, workspaces, members, invitations and the authorization policy.
  `Authenticate` and `AuthorizeWorkspace` keep their contract; `Principal`
  gains `Token` and `Session`, `Workspace` gains `State`, `Role` and
  `CreatedAt`. `NewIdentity` takes a `Config` (public URL, GitHub App).
- `internal/notifications`: the email outbox. `Enqueue` and `Discard` run in
  the caller's transaction. The scheduler delivers through Resend with the
  message id as idempotency key, 8 attempts with backoff from 10 s doubling to
  a 10 minute cap, a 2 minute lease fenced by the attempt number, and purges
  bodies 2 days after settling. The server records delivery reports from
  `/webhooks/resend` (Svix signature, 5 minute window, older events never
  replace newer ones).
- `internal/api`: identity handlers (`identity.go`), browser sign-in
  (`browser.go`), the webhook (`webhooks.go`); `handler.go` resolves a bearer
  token or the session cookie and lets each operation's OpenAPI security
  requirement decide, so operations are authenticated unless the document
  says `security: []`.
- `cmd/scheduler/accounts.go`: email delivery, housekeeping (expired sessions
  and device codes, body purge) and the workspace deletion coordinator.
- `python/lazycloud`: device-code `login`, `workspace` commands on the new
  API, the out-of-date notice; the old `WorkspaceControlClient`,
  `shared.http.workspaces` and `shared.identity` are deleted.

## Decisions

- PostgreSQL is the only authority. Sign-in state (state, PKCE verifier,
  return path) lives in a `__Host-` HttpOnly cookie for 10 minutes; sessions,
  device codes, tokens, invitations and emails are rows. No Redis.
- The browser session is an HttpOnly, Secure, SameSite=Lax `__Host-` cookie
  naming a `sessions` row for 12 hours. Unsafe requests authenticated by the
  cookie must carry `Origin` equal to `LAZYCLOUD_PUBLIC_URL`.
- First sign-ins of one GitHub account serialize on an advisory lock; the
  account and its owned workspace (named from the login, `-2`..`-9` on
  collision) commit together.
- Device codes follow RFC 8628: `XXXX-XXXX` from a consonant alphabet, 15
  minute expiry, 5 second interval, `slow_down` adds 5 seconds when polled
  early. Approval needs an account credential (not a workspace-restricted
  token). The approving poll mints the device token and consumes the code in
  one transaction.
- Tokens keep `lc_` + SHA-256. Account tokens expire after 1 to 90 days or
  never. `last_used_at` is collected in memory and written in one batched
  `UPDATE` every 30 seconds, so authentication is one `SELECT`.
- Roles: owner, administrator, member. Only platform administrators create
  and delete workspaces (as in the reference); any member renames;
  administrators invite, change roles and remove; members leave; the owner is
  neither removed nor demoted. Platform administrators act without
  membership.
- Deletion: one transaction marks the workspace `deleting` (serialized so the
  last active workspace stays), revokes its restricted tokens and deletes its
  invitations and their unsent emails; every request but `GET`/`DELETE` of
  it is refused. Planning treats its releases as stopping, the coordinator
  cancels its running tasks through `Execution.CancelTask`, and images fails
  the builds it started and hands finished ones to another workspace that
  resolved the image. Once no workload or build container is live, storage
  deletes its objects and identity deletes the row (cascading) under the
  workspace row lock, re-checking for live containers so a concurrent
  container insert keeps it for a later pass. `DELETE` again resumes it.
  Every step is idempotent. Answers to invitations lock the workspace before
  the invitation, as deletion does.
- Plan limits belong to billing: `admitMember` (invite and accept) and
  `admitWorkspace` (creation) run inside those transactions and admit
  everything today.

## API

Public (contracts/openapi.yaml, identity section):
`GET /v1/me`, `DELETE /v1/sessions/current`,
`GET|POST /v1/tokens`, `DELETE /v1/tokens/{token}`,
`POST /v1/device-codes`, `POST /v1/device-codes/token` (both unauthenticated),
`GET /v1/device-codes/{user_code}`, `POST .../approve`, `POST .../deny`,
`GET|POST /v1/workspaces`, `GET|PATCH|DELETE /v1/workspaces/{workspace}`,
`GET /v1/workspaces/{workspace}/members`,
`PATCH|DELETE /v1/workspaces/{workspace}/members/{user}`,
`GET|POST /v1/workspaces/{workspace}/invitations`,
`DELETE /v1/workspaces/{workspace}/invitations/{invitation}`,
`POST .../invitations/{invitation}/resend`,
`GET /v1/invitations/{token}`, `POST .../accept`, `POST .../decline`.

Outside the document: `GET /auth/github/start?return_to=`,
`GET /auth/github/callback` (redirects to `return_to`, `/dashboard`, or
`/signin?error=access_denied|invalid_state|invalid_return_to|account_disabled|provider_unavailable|provider_refused`),
`POST /webhooks/resend`. Every response carries
`X-Lazycloud-Recommended-Client-Version` when
`LAZYCLOUD_CLIENT_RELEASE_VERSION` is set.

Configuration: server `LAZYCLOUD_PUBLIC_URL`, `LAZYCLOUD_GITHUB_CLIENT_ID`,
`LAZYCLOUD_GITHUB_CLIENT_SECRET`, `LAZYCLOUD_RESEND_WEBHOOK_SECRET`,
`LAZYCLOUD_CLIENT_RELEASE_VERSION`; scheduler `LAZYCLOUD_RESEND_API_KEY`,
`LAZYCLOUD_RESEND_FROM` and the object store variables.

## Progress

- [x] Migration 0002
- [x] Identity: users, sign-in, sessions, tokens, device codes
- [x] Identity: workspaces, members, invitations, deletion
- [x] Notifications outbox, Resend sender, webhook, purge
- [x] OpenAPI paths and handlers, cookie auth, client version header
- [x] Scheduler loops: email delivery, housekeeping, workspace deletion
- [x] Python: login device flow, workspace commands, version notice
- [x] Tests, lint, measurements
- [ ] Real GitHub App and Resend account (acceptance gap, below)

## Evidence

Go (real PostgreSQL 18, Garage; `go test -race`, repeated 10 times for
identity): `internal/identity` TestGitHubSignIn, TestSignInRefusals,
TestConcurrentFirstSignIn, TestSignOutEndsOnlyThatSession, TestDeviceLogin,
TestAccountTokens, TestWorkspaceAuthorization, TestWorkspaceLifecycle,
TestConcurrentDeletionKeepsOneWorkspace, TestMembers, TestInvitations,
TestConcurrentAcceptIsSingleUse, TestAnswerRacingDeletionDoesNotDeadlock; `internal/notifications`
TestDeliveryRetriesAndFailures, TestBackoffIsCapped, TestLeaseFencesStaleSettle,
TestDiscardPurgeAndNoSender, TestWebhookSignature; `internal/api`
TestBrowserSessionAndDeviceLogin, TestSignInFailuresRedirect,
TestIdentityOperationsOverHTTP, TestResendWebhook; `cmd/scheduler`
TestWorkspaceDeletion, TestWorkspaceDeletionWithImageBuilds. GitHub and Resend are httptest stubs at the provider
boundary only (`internal/identity/identitytest`).

Python (`pytest -x python`, 356 passed after merging go-rewrite): test_sdk_workspace_cli.py (all on
the HTTP fake API), test_sdk_platform_api.py
test_login_without_a_token_runs_the_device_flow,
test_device_login_reports_denied_and_expired,
test_login_verifies_the_token_and_stores_the_owned_or_only_workspace,
test_every_command_tells_an_out_of_date_client_to_update.

Local run (server and scheduler on a private database, ports 18480/18481):
`lazycloud login` printed the card, an admin token approved the code with
`curl`, and the CLI stored the device token and workspace `home`;
`workspace create/list/rename/use/delete` worked, the invitation email
stayed `queued` without Resend, the invitee previewed and accepted once (the
second accept answered 404), and the scheduler removed the deleted workspace
8 ms after the request through the `lc_workspace` wake.

## Measurements

Local, one host, sequential keep-alive requests:

| Request | p50 | p95 |
| --- | --- | --- |
| `GET /v1/me` (1,000) | 0.46 ms | 1.13 ms |
| `GET /v1/workspaces/{ws}` (1,000) | 0.38 ms | 0.96 ms |
| `GET /v1/tokens` (500) | 0.47 ms | 1.06 ms |
| `POST /v1/device-codes` (200) | 1.01 ms | 1.34 ms |
| `POST .../invitations` with outbox row (200) | 1.04 ms | 2.68 ms |

An authenticated request costs one `SELECT` for the credential and one for
workspace access; token use adds no write per request. Device login waits
at most one poll interval (5 s) after approval. Workspace deletion with no
live containers finishes in under 10 ms after the request; with containers
it waits for their hosts to stop them. The reference cached tokens in
memory with Redis invalidation and wrote `last_used_at` per authentication.

## Intentional differences from the reference

- The `/callback` page exchange is gone: the server callback sets the
  HttpOnly session cookie and redirects to `return_to`, which the dashboard
  sets to `/callback#code=<destination>`; the page clears the fragment,
  confirms the session with `/v1/me` and goes to the destination or
  `/dashboard`. The reference's fragment carried a code the
  page traded for a token kept in JavaScript; with an HttpOnly cookie no
  token reaches the page.
- A first GitHub sign-in whose verified primary email matches an account
  without a GitHub identity links to it, so `server admin create-user
  --admin` bootstraps an administrator who then signs in. The reference
  linked only by a pre-recorded GitHub user id.
- `lazycloud login` without `--workspace` stores the account's owned
  workspace, else its only one. The reference stored none and let the
  server resolve it per request; paths now name the workspace. An account
  that owns none and belongs to several fails with "Choose a workspace for
  this profile" and a hint to pass `--workspace`, since the server has no
  current workspace to fall back on.
- `workspace rename` with no workspace in the profile fails with "No
  workspace is selected" and a hint to run `workspace use`, for the same
  reason.
- `workspace delete` returns once deletion has begun ("Deleted X; its data is
  removed in the background"), because hosts stop containers asynchronously.
  The reference held the request until cleanup finished.
- Accepting or declining an invitation also withdraws its unsent email.
- Workspace audit history is not carried over; no parity item shows it.
- Settings → Tokens drops the "Disabled by admin" line (tokens have no
  admin disable) and shows `expired` on expired tokens, which the reference
  listed as `active`.
- `workspace list` shows a workspace being deleted as `name (deleting)`, and
  workspace `--json` follows the API `Workspace` (`state`, `role`).
- Emails say "LazyCloud"; the reference's title-cased "Lazycloud" was a typo
  of the product name.

## Gaps

- No GitHub App or Resend credentials exist locally, so real GitHub sign-in
  and real email delivery are unverified; locally emails stay `queued` and
  `/auth/github/start` redirects to `provider_unavailable`.
- Admin user management (role, disable) and the unfunded-storage email are
  outside these sections (Dashboard admin settings; billing).
- An upload presigned before a workspace's deletion and finished afterwards
  leaves bytes under the deleted workspace's prefix.
- Dashboard pages (`/signin`, `/activate`, `/invitations/$token`, settings,
  members, deletion recovery) come with the web packet.
- In-container calls for a deleting workspace must be refused by the
  container API (workload-runtime packet) the same way `AuthorizeWorkspace`
  refuses them.
