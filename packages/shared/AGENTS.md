# Shared contracts

- Own backend-free models, precise types and deterministic helpers. No database/
  Redis clients, FastAPI state, process entrypoints or SDK session behavior.
- JSON contracts live by domain under `shared.http` and extend `HttpModel`.
  Use typed payloads and deliberate exports; remove unused models and duplicate
  request/body shapes. Errors have their existing domain/transport owners.
- Missing deployment coordinates raise `MissingDeploymentSettingError` naming the
  variable. Never silently select localhost or another datastore.
- Expose published pricing from the canonical rate card; clients keep no copy.
  Resolve workload defaults centrally, preserving omitted versus explicit values.
- Reservation and burst ceilings differ. Purchase, reservation and placement use
  the same fit helpers and observed node memory.
- Preserve cgroup protection, throttle, hard-limit and swap semantics. Sandbox
  overhead corrections require live measurements and consistent application;
  do not invent constants or clamp limits against host-wide values.
