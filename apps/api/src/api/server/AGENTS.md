# API Server

Keep routers thin and grouped by resource. Use standard FastAPI dependencies,
typed shared request/response models, app state, lifespan, and background tasks.
Shared dependencies and auth live under `api.server`; do not create per-router
auth aliases, custom router builders, or app-local response dump helpers.

Workspace scope comes from bearer auth with the authorized optional override.
Exercise changed routes through their real authorization, service, response,
and failure mapping; do not inventory route metadata as evidence.
