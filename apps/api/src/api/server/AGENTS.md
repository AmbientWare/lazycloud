# API Server

Routers, shared dependencies, and authentication for the HTTP surface.

Keep routers thin and grouped by resource: validate, authorize, call one
service, map the typed result. Use the framework's own dependencies, typed
shared request and response models, app state, lifespan, and background tasks
rather than local reimplementations—no per-router auth aliases, custom router
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

Membership is read in exactly one place, `authorize_token_workspace`. A user
credential reaches a workspace only through a row naming them, and resolving that
in the shared dependency rather than in the routes that remembered to ask is what
keeps the rule identical on every path. A request that names no workspace resolves
the default and is then checked against membership: guessing among the workspaces
a person holds would sometimes act on the wrong one silently, where this refuses
and says why.

Resources a person owns rather than a workspace—their connected cloud account,
their registered domains—take `read_user`/`write_user`. A workspace-scoped
automation token deliberately fails there: it carries no authority over the
account that owns the workspace it was minted for.
