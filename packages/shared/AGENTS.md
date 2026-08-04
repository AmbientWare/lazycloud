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

## Settings Layers

`shared.settings.YamlLayeredSettings` gives a `BaseSettings` class a YAML layer
under the environment: `init > env > yaml > secrets`. The environment holds
secrets and whatever legitimately differs per environment or changes while a
deployment runs; YAML holds what is normally identical everywhere, so the
environment has to win. Facts derived from a build belong to neither.

`LAZYCLOUD_CONFIG_DIR` addresses one directory holding one file per owner, and
each settings class names its file and its dot-notation section. The variable has
no default, so unset reads no YAML at all—a config file a machine happens to
carry can never decide what a process or the suite observes. Within that:

- A directory that is named but absent fails loudly. An unmounted volume would
  otherwise leave every owner on its defaults while the deployment reads as
  configured.
- A missing owner file, or a missing section in a present file, contributes
  nothing. One directory serves every owner and a file states only what it
  overrides, so absence is not an unsatisfied dependency.
- Exactly one file is read per owner, so the shallow merge `YamlConfigSettingsSource`
  performs across files never applies. Merging across sources is deep, which is
  what lets one environment variable override one YAML key without erasing its
  siblings.
- Adopting classes use `extra="forbid"` so a mistyped YAML key is rejected rather
  than dropped. The environment source only ever emits declared fields, so
  forbidding extras costs a settings class nothing when several share an
  `env_prefix`.
- `env_only_fields`, plus every `SecretStr`/`SecretBytes` field automatically,
  may not appear in YAML; naming one there raises without echoing its value.
