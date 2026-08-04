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
