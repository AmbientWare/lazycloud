# Shared contracts

- Own Python consumer contracts, precise types and deterministic helpers. No
  backend policy, database/Redis clients, server state or SDK session behavior.
- JSON contracts live by domain under `shared.http` and extend `HttpModel`.
  Use typed payloads and deliberate exports; remove unused models and duplicate
  request/body shapes. Errors have their existing domain/transport owners.
- Missing deployment coordinates raise `MissingDeploymentSettingError` naming the
  variable. Never silently select localhost or another datastore.
- Follow the new language-neutral wire contracts as they are implemented. Old
  Python models may change or disappear with their consumers; backward
  compatibility is not required. Do not maintain competing Python/Go schemas.
- Fetch pricing and policy from backend owners. Keep client defaults limited to
  authoring concerns and preserve omitted versus explicit values.
- Existing scheduling and resource helpers are reference code to retire as their
  consumers change, not a second implementation for the new Go backend.
