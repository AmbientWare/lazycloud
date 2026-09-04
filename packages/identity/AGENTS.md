# Identity package

Users, external identities, sessions, authorization, tokens, bootstrap, and
workspace membership.

Authorization decisions stay deterministic and inspectable: the same principal
and the same resource yield the same answer, with no ambient state quietly
deciding it. That is why `decide_authorization` takes the membership row rather
than reading it. Whoever builds the requirement does the lookup, so a decision
can be logged and replayed without a database. Durable access uses explicit
database repositories, and runtime context arrives through narrow protocols.

Authorization rests on tenant scope and on the distinctions between admin,
worker, and machine tokens. Preserve them explicitly rather than deriving them.
Treat token scope, token kind, revocation, forgery resistance, single use, and
secret non-disclosure as separate properties that each have to hold on their own.

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

The platform mints workspace principals for the things it runs, such as a
worker, a machine, an agent slot, or a workspace's primary automation credential,
and no route lets a person create one. That is what makes the kind meaningful: a
credential naming a workspace is one nobody chose, so its blast radius is the one
the platform gave it.

Revocation is terminal. A revoked credential is never reactivated, and its row
stays as the account's only record that it existed. Only the platform's own
credentials are ever removed outright. A service credential re-minted under the
same name replaces its predecessor, and expiry pruning clears the rest.

Administrator standing has one definition, `AuthService.platform_role`. Both the
admin token kind and an account whose role says so confer it, and every caller
asks there so the answer cannot differ between the authorization decision and a
route that branches on it.

The platform always keeps at least one active administrator. `UserService`
refuses to demote or disable the last one, deciding from the set of
administrator rows it locks rather than from a count read a moment earlier, so
two writes each removing the other of the last two serialize and the second
sees the first. Disabling counts as demoting because `platform_role` reads a
disabled administrator as a member. The offline bootstrap writes through the
repository and is deliberately outside the rule, because it is how an
installation that has lost every administrator gets one back.

## Accounts and membership

A workspace has exactly one member with the `owner` role, held by a partial
unique index rather than by convention. The owner is who a workspace's connected
compute and registered domains resolve through, so a second one would make
"whose account backs this workspace" have two answers.

## Invitations

An invitation is a link. Whoever opens it, signed in as any account, joins, and
the membership binds to that account. The address the message went to decides
nothing about who may accept, because an address can be changed on the far side
and reassigned to somebody else, so an offer keyed on one would follow the
address rather than the person it was written for.

What protects the offer is the secret in the link: 32 bytes, stored only as its
SHA-256 so a database dump is a list of offers rather than a set of working keys,
redeemed under a row lock, and replaced whenever the offer is sent again. That
last part is why a resend is safe: the message went astray once, and leaving the
old link live would leave whatever it went astray into holding a way in. Holding
the link is the claim being made, so forwarding one hands somebody else a seat,
and the message says so.

Only open offers are rows. Accepting, declining and revoking each delete theirs,
because the membership records an acceptance and the workspace audit history
records every outcome; a table that also kept answered offers would be a second,
slower account of the same events with nothing keeping the two in step. Whether
an offer has expired is decided here against one clock and published as a field,
never recomputed by whoever renders it, or two people looking at one workspace
would disagree about which offers are live.

Membership rows are written only at acceptance, so nothing reading
`workspace_members` can mistake an offer for access. One open offer per address
per workspace: a second invite is refused and names the resend instead.
Accepting an offer carrying more authority than a membership someone was
meanwhile given raises the role to what was offered, because the offer is a live
administrator decision rather than a formality to consume.

An open offer holds a seat. Admission counts it alongside the members it would
join, so a plan with one seat left refuses the second invitation rather than
sending five emails and turning four people away at the door, where the refusal
reaches somebody who cannot act on it.

Nothing sends the message inline. The invitation row and the queued message
commit together and `notifications` delivers it, so no request waits on an email
provider, and there is no offer nobody was told about and no message about an
offer that rolled back.

## Signing in

A person signs in through an external identity provider, and `user_identities`
records which external account reaches which of ours. The link is keyed on the
provider's own immutable subject, GitHub's numeric user id, and never on a login
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

Sign-in completes only when every piece of provisioning it was handed succeeds.
Each one is a collaborator composition injects and is idempotent, so a failure is
repaired at the next attempt. Until it is, the exchange code is never minted, so
no session exists and the half-provisioned account cannot run anything. What
that provisioning is, and what it holds while it runs, belongs to whoever
composes it rather than here; this package states the shape it calls and the
ordering it guarantees, and nothing about the other side of the call.

The provider access token is used once to read the profile and then discarded. It
never crosses the `ExternalIdentityProvider` boundary, which is what keeps every
caller above it from becoming somewhere it could be logged or stored.
