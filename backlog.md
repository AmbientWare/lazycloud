# Backlog

Thoughts worth keeping that are not tracked work. A ticket is for something with
an owner and acceptance; this is for the ones that are still just a good idea.
Move an item onto the board when it becomes real, and delete it when it stops
being true.

## Session credentials live in localStorage

The dashboard holds its bearer in `localStorage["lazycloud_web_token"]`. Any XSS
on the origin reads it and replays it from anywhere for the session's full 12
hours; an httpOnly cookie could be ridden from the page but not stolen outright.
The trade is real and OWASP leans to the cookie.

Moving would mean setting the cookie at redemption, deleting the localStorage
store and its four read sites, and adding CSRF protection on mutations. The
resumable event stream would get *simpler* — `EventSource` cannot set a header,
which is the only reason `sse.ts` reimplements it over `fetch`.

## Open signup has no abuse control

Any GitHub account can sign in and get a workspace that schedules real compute.
`user_identities.provider_account_created_at` already records the GitHub account
age at link time, so an age floor is cheap whenever it is wanted. The real lever
is a default quota on the first workspace, which belongs with the tier work.

## The bootstrap admin token is a static secret in `.env`

`LAZYCLOUD_TOKEN` is configured rather than minted, which is what lets every
Compose step know the credential in advance without reading anything back. The
cost is a long-lived platform-admin secret sitting in a dotfile, unrotated across
resets.

The alternative already half-exists: `auth bootstrap --output` mints one and
publishes it to the `lazycloud-administrator-token` volume. Downstream steps
would read the file instead of the environment. The host shell loses `grep .env`
as a way to get a credential, which is the main thing to solve before doing it.

## Revoking the GitHub App does not end the session

A person who revokes our App on GitHub keeps a working dashboard session until it
expires. GitHub sends `github_app_authorization` on revocation; consuming it and
calling the existing session revocation would close the gap. Needs a webhook
endpoint and the App's webhook turned back on.

## PKCE enforcement is unproven

We send `code_challenge` with `S256` and GitHub accepts it, but GitHub does not
require PKCE and nothing here has confirmed it *rejects* a mismatched verifier.
If it silently ignores the challenge, the code reads as protection it is not
providing. One deliberate wrong-verifier exchange against the real App settles
it.

## `/dashboard` assumes the account owns a workspace

`DashboardEntry` does `target.name` on `workspaces[0]` with no guard. Every path
that creates an account also creates a workspace, so it holds today — but an
account that lost its last membership renders a crash instead of an explanation.

## Deleting a workspace leaves its bucket behind

Removing a workspace row leaves `workspace-<id>` in the object store. Harmless
until it isn't: buckets accumulate and nothing names them as garbage.

## Six `.env` keys are undocumented

`AWS_PROFILE`, `LAZYCLOUD_AWS_CONTROL_STACK_NAME`,
`LAZYCLOUD_AWS_CAPACITY_GPU_AMI_IDS`, and three `LAZYCLOUD_E2E_*` are set locally
but absent from `.env.example`, so a fresh clone cannot reproduce the setup. They
sit in a labelled section at the bottom of `.env` until someone folds them in.

## Playwright specs are not run anywhere

`apps/web/tests/e2e/*.spec.ts` are in no workflow. They seed a token into
localStorage and never render a signed-in user, so they may already be failing.
Either wire them into CI or delete them; an unrun suite is worse than neither.
