# Identity Package

Users, passwords, sessions, authorization, tokens, bootstrap, and workspace
membership.

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

Passwords are PBKDF2-HMAC-SHA256 with their own iteration count, separate from
the token work factor because a password is chosen by a person and a token is
256 bits of urandom. The minimum length is eight characters. Authentication costs the same whether the username exists or
the password is wrong; telling those apart is how an attacker enumerates accounts.
A password change revokes the sessions minted under the old one, and the caller
publishes that revocation so no replica's cache outlives it.
