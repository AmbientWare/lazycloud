# API App

Own FastAPI construction and request/process lifetimes. Compose settings,
repositories, clients, and services here; reusable workflows remain in domain
packages and SDK abstractions never enter composition.

Central handlers map `shared.errors.DomainError` by exception MRO. Register
domain mappings centrally, not per route. Register API routers before
`api.web_static`, whose SPA fallback must exclude `/api/*`. Accept composition
changes with the affected real request, lifespan, authorization, and cleanup.
