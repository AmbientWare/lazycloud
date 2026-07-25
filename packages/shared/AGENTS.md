# Shared Package

Own backend-free boundary models and protocol-neutral primitives. No SQLAlchemy,
Redis clients, FastAPI app state, process entrypoints, or SDK session behavior.
Use Pydantic v2, precise enums/types, deterministic helpers, and deliberate
exports.

JSON contracts live by domain under `shared.http`, extend `HttpModel`, and use
typed dynamic payload semantics rather than broad JSON bags. Domain errors live
in `shared.errors`; transport errors in `shared.http.errors`. Do not create
Body/Request twins, duplicate path fields in bodies, soft-error envelopes, or
initializer compatibility facades. Accept contract changes through one
authoritative producer/consumer pair with rejection, serialization-loss, and
security evidence as applicable.
