# Shared contracts

- Own Python consumer contracts, precise types and deterministic helpers. No
  backend policy, database clients, server state or SDK session behavior.
- JSON contracts live by domain under `_shared.http` and extend `HttpModel`.
  Use typed payloads and deliberate exports; remove unused models and duplicate
  request/body shapes. Errors have their existing domain/transport owners.
- Follow the language-neutral wire contracts; API models are generated into
  `lazycloud.contracts`. Do not maintain competing Python/Go schemas.
- Fetch pricing and policy from backend owners. Keep client defaults limited to
  authoring concerns and preserve omitted versus explicit values.
- Resource and placement helpers describe what the SDK sends; scheduling
  decisions stay in the backend.
