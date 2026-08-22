# Public SDK

`lazycloud` is the backend-free user SDK and the public CLI: everything a user of
the platform imports or runs.

It may depend on `shared` and on nothing else in this repository: never apps,
backend domains, SQLAlchemy, Redis, FastAPI, schedulers, or workers. That
constraint is what makes it installable beside arbitrary user code, so it is a
boundary rather than a preference.

Keep the decorator and resource APIs ergonomic, low-level clients under
`lazycloud.clients`, bound high-level workflows under `lazycloud.session`, and
shared public commands under `lazycloud.cli`. Clients speak the current public
HTTP contracts; transport failures raise `HttpApiError`, and abstractions
translate those once into typed operation errors.

Use explicit client injection rather than ambient construction, and delete old
import paths instead of aliasing them. A public API is only cheap to keep honest
while it is still small.
