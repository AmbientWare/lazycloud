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

Keep both. A workspace-scoped token is the only blast-radius control there is: if
every credential were account-wide, a leaked CI token would reach production as
readily as a scratch workspace. Interactive and CLI use takes a user credential;
automation takes a workspace one.

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
256 bits of urandom. Authentication costs the same whether the username exists or
the password is wrong; telling those apart is how an attacker enumerates accounts.
A password change revokes the sessions minted under the old one, and the caller
publishes that revocation so no replica's cache outlives it.
