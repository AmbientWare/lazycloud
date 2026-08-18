# Shared Package

Backend-free boundary models and protocol-neutral primitives—the vocabulary every
other owner speaks.

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
a validator raises with the variable named. The marker is not a fallback—nothing
reaches it—it exists because a checker synthesizes the constructor from the
fields and would otherwise demand the value at every call site that means to read
it from the environment.

Do not create request and body twins, duplicate a path field inside a body, add
soft-error envelopes, or keep initializer compatibility facades. A contract
shared by several owners is only worth its cost while there is exactly one of it.

The published rate card is a contract with a consumer that cannot import it: the
pricing page renders in a browser without calling the API, so the card has to
reach it as source. `shared.billing_rate_card_typescript` renders that source, and
lives here rather than in `apps/web` or `apps/cli` because the alternative is a
second copy of every price maintained by hand, and the drift a customer finds by
being charged something the page did not say. It is a deterministic function of
the card and nothing else—no clock, no locale, no environment—because the check
that keeps the generated file current is a byte comparison against it.

A workload's defaults are resolved by kind, and a schedule is not a kind. It is
answered separately, in `resolve_keep_warm_seconds`, because it says something
about one deployment rather than about a category of them: a scheduled function
keeps no idle window unless its author names one.

That only works while an omitted value stays distinguishable from a chosen one,
which is why the SDK sends nothing rather than the default it would have picked.
A client that fills in defaults leaves the resolver with nothing to resolve.
