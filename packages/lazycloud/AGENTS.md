# Public SDK

`lazycloud` is the backend-free user SDK and public CLI. It may depend on
`shared`, never apps, backend domains, SQLAlchemy, Redis, FastAPI, schedulers,
or workers. Keep decorator/resource APIs ergonomic, low-level clients under
`lazycloud.clients`, bound high-level workflows under `lazycloud.session`, and
shared public commands under `lazycloud.cli`.

Clients use current `/api/v1` and `/gateway` contracts. Transport failures raise
`HttpApiError`; abstractions translate them into typed operation errors. Delete
old import paths and use explicit client injection. Accept changed public calls
against their real HTTP/control owner, preserving serialization, targeting,
timeouts, error mapping, and secret handling.
