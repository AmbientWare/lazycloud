# Shared Package

Backend-free boundary models and protocol-neutral primitives—the vocabulary every
other owner speaks.

No SQLAlchemy, Redis clients, FastAPI app state, process entrypoints, or SDK
session behavior. Use Pydantic v2, precise enums and types, deterministic
helpers, and deliberate exports.

JSON contracts live by domain under `shared.http` and extend `HttpModel`, with
typed payload semantics rather than broad JSON bags. Domain errors live in
`shared.errors`, transport errors in `shared.http.errors`.

Do not create request and body twins, duplicate a path field inside a body, add
soft-error envelopes, or keep initializer compatibility facades. A contract
shared by several owners is only worth its cost while there is exactly one of it.
