# Shared package

Backend-free boundary models and protocol-neutral types and helpers: the
vocabulary every other owner speaks.

No SQLAlchemy, Redis clients, FastAPI app state, process entrypoints, or SDK
session behavior. Use Pydantic v2, precise enums and types, deterministic
helpers, and deliberate exports.

JSON contracts live by domain under `shared.http` and extend `HttpModel`, with
typed payload semantics rather than broad JSON bags. Domain errors live in
`shared.errors`, transport errors in `shared.http.errors`, and a deployment fact
nobody supplied raises `MissingDeploymentSettingError` from
`shared.deployment_settings`.

Which datastore a process talks to is not a default. A settings class that
answers `localhost` when the environment is silent points the process at whatever
happens to be listening and reports success, and the wrong database is found by
its consequences rather than by its error. Connection coordinates state their own
absence instead: the field carries a marker no address can be confused with, and
a validator raises with the variable named. The marker is not a fallback, and
nothing reaches it. It exists because a checker synthesizes the constructor from
the fields and would otherwise demand the value at every call site that means to
read it from the environment.

Do not create request and body twins, duplicate a path field inside a body, add
soft-error envelopes, or keep initializer compatibility facades. A contract
shared by several owners is only worth its cost while there is exactly one of it.

The published rate card reaches browsers through `shared.http.pricing`, which
derives the public catalog directly from `shared.billing_rate_card`. Web clients
validate that response and keep no compiled or hand-maintained copy of plan
terms, entitlements, or rates.

A workload's defaults are resolved by kind, and a schedule is not a kind. It is
answered separately, in `resolve_keep_warm_seconds`, because it says something
about one deployment rather than about a category of them: a scheduled function
keeps no idle window unless its author names one.

That only works while an omitted value stays distinguishable from a chosen one,
which is why the SDK sends nothing rather than the default it would have picked.
A client that fills in defaults leaves the resolver with nothing to resolve.

A container's memory reservation and its ceiling are deliberately different
numbers, and the gap between them is the product. Placement reserves 1.25x the
request so a node is never oversubscribed on what was promised; the ceiling lets
a container grow several times past it into memory nobody reserved. beta9 keeps
the two equal, which is safe by construction and means no burst at all. We do
not, which is why eviction has to exist: the gap is only survivable because
something chooses who leaves when it closes.

Under gVisor the sentry and the gofer are charged to the container's cgroup
alongside guest memory, so every value below is really "guest plus sandbox" and a
small container is protected for less than it asked for. No constant covers it
yet, deliberately: the footprint depends on file access and thread count, and two
figures here that were chosen rather than measured both turned out wrong in a
direction nobody noticed. Measure it on a live sandbox before applying one: RSS
against guest usage, at rest and under load. Apply it to all three values rather
than the hard limit alone.

Four cgroup values express it. `memory.low` is the request and is what reclaim
protects. `memory.high` is the ceiling and throttles rather than kills.
`memory.max` is the wall behind it, clamped to what the machine holds because a
ceiling larger than the node is one the container never reaches. The host runs
out first and its OOM killer picks by size. Swap is the fourth and the other
three are decorative without it: a cgroup of anonymous pages with nowhere to
reclaim to does not slow at `memory.high`, it stalls, measured at 21 seconds for
an 8MiB allocation with zero pages scanned.
