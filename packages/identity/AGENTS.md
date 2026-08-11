# Identity Package

Users, external identities, sessions, authorization, tokens, bootstrap, and
workspace membership.

Authorization decisions stay deterministic and inspectable: the same principal
and the same resource yield the same answer, with no ambient state quietly
deciding it. That is why `decide_authorization` takes the membership row rather
than reading it—whoever builds the requirement does the lookup, so a decision can
be logged and replayed without a database. Durable access uses explicit database
repositories, and runtime context arrives through narrow protocols.

Tenant scope and the distinctions between admin, worker, and machine tokens are
load-bearing—preserve them explicitly rather than deriving them. Treat token
scope, token kind, revocation, forgery resistance, single use, and secret
non-disclosure as separate properties that each have to hold on their own.

## Principals

A token names exactly one principal, and the schema enforces it:

- A **user** principal (`Admin`, `User`, `Session`) reaches every workspace its
  owner is a member of, resolved per request from `workspace_members`.
- A **workspace** principal (`WorkspacePrimary`, `Workspace`,
  `WorkspaceRestricted`, `Worker`, `WorkerPrivate`, `Machine`) reaches the single
  workspace it was minted for.

Every credential a person can create is a user principal: signing in mints a
`Session`, and the tokens tab mints a `User`. Both reach every workspace the
account belongs to, because a person's authority follows their membership rather
than the workspace they happened to be looking at when they asked.

Workspace principals are minted by the platform for the things it runs — a
worker, a machine, an agent slot, a workspace's primary automation credential —
and no route lets a person create one. That is what makes the kind meaningful: a
credential naming a workspace is one nobody chose, so its blast radius is the one
the platform gave it.

Revocation is terminal. A revoked credential is never reactivated, and its row
stays as the account's only record that it existed. Only the platform's own
credentials are ever removed outright — a service credential re-minted under the
same name replaces its predecessor, and expiry pruning clears the rest.

Administrator standing has one definition, `AuthService.platform_role`. Both the
admin token kind and an account whose role says so confer it, and every caller
asks there so the answer cannot differ between the authorization decision and a
route that branches on it.

## Accounts And Membership

A workspace has exactly one member with the `owner` role, held by a partial
unique index rather than by convention. The owner is who a workspace's connected
compute and registered domains resolve through, so a second one would make
"whose account backs this workspace" have two answers.

## Signing In

A person signs in through an external identity provider, and `user_identities`
records which external account reaches which of ours. The link is keyed on the
provider's own immutable subject — GitHub's numeric user id — and never on a login
or an email address. Both of those can be renamed, released, and re-registered by
somebody else, so keying on either is how one person ends up signed in as another.
Email is stored without a unique constraint for exactly that reason.

An account with no identity row cannot sign in and exists to own tokens. The
offline bootstrap administrator is one, deliberately: it is the credential that has
to work when the identity provider is what is broken, so it must never acquire a
dependency on one. Only an administrator can mint a credential naming another
account, and that is the only way such an account gets its first one.

Sign-in puts no credential in a URL. The provider callback mints a single-use
exchange code, and the session is minted when that code is redeemed, so an
abandoned tab leaves nothing live behind. Redemption also requires the cookie set
when the flow began, which is what makes a code read out of browser history
useless. Both the state and the exchange code are stored under a hash of
themselves, consumed with GETDEL before anything is validated, so a malformed
payload burns the credential rather than leaving it replayable.

The provider access token is used once to read the profile and then discarded. It
never crosses the `ExternalIdentityProvider` boundary, which is what keeps every
caller above it from becoming somewhere it could be logged or stored.
