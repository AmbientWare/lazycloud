# API server

Routers, shared dependencies, and authentication for the HTTP API.

Keep routers thin and grouped by resource: validate, authorize, call one
service, map the typed result. Use the framework's own dependencies, typed
shared request and response models, app state, lifespan, and background tasks
rather than local reimplementations. No per-router auth aliases, custom router
builders, or app-local response serializers.

Workspace scope comes from bearer authentication, and the override is available
only to callers authorized to use it. Authorization belongs in the dependency
that every route shares, not in the routes that remembered to ask.

Provisioning units are internal capacity rather than a customer concept, so
every `/api/v1/units` route requires `admin_access` and takes its workspace from
the request instead of the caller's token. Customers reach their own hardware
through compute policy, compute instances, and machine join. An operator route
that read workspace from the bearer token would let a customer-triggered delete
strand provider capacity nothing durable can name.

Units are addressed by id, never by name. `pool` names the scheduling pool a
workload asks for, and several units feed one pool; a route keyed on a name
could resolve a unit through a value that meant a pool, which is how the two
were confused before they were separated.

Every bearer-authenticated request resolves its workspace through
`authorize_token_workspace`: the shared dependency calls it for a route that takes
the workspace from its query, and a route that names the workspace in its path
calls it directly rather than deciding for itself. A user credential reaches a
workspace only through a membership row naming them, and keeping that read in one
function is what makes the rule identical on every path. A request that names no
workspace resolves the default and is then checked against membership. Guessing
among the workspaces a person holds would sometimes act on the wrong one
silently, where this refuses and says why.

The shell WebSocket ticket is the one credential that does not arrive as a bearer
header, so it carries its own membership read in `identity.websocket_tickets`
against the workspace the ticket was minted for. Keep that decision equivalent to
this one; a ticket is redeemed once and cannot fall back to the header path.

Resources a person owns rather than a workspace, such as their connected cloud
account, their registered domains, and the machines they join, take
`read_user`/`write_user`. A workspace-scoped automation token deliberately fails
there: it carries no authority over the account that owns the workspace it was
minted for.
