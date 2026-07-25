# Opt-In End-to-End Scenarios

Each file proves one production capability through its public SDK, CLI, API, or
browser path. Scenarios consume an already-prepared environment; they never
build, deploy, migrate, reset, or inventory the platform.

- `local/` targets the healthy canonical root Compose stack.
- `external/` targets an explicitly authorized provider, cluster, Tailnet, or
  other external system.
- Ordinary `pytest` and changed-scope validation must not execute this tree.
  Run Python scenarios as exact modules from the repository root
  (`uv run python -m tests.e2e...`) and browser scenarios as exact nodes. Do
  not execute Python files by path or add import-path bootstrap code.
- Declare exact prerequisites and target guards. Exit `77` when a prerequisite
  is unavailable, `0` only after terminal production evidence, and nonzero for
  any mutation, assertion, or cleanup failure.
- Create uniquely named resources, clean them through their public owner, and
  verify only their public terminal state. Never delete fixed or pre-existing
  resources.
- Keep fixtures beside their one consumer. Shared support is limited to small
  secret-safe process and live-gate primitives; do not create a workflow,
  lifecycle, polling, deployment, or cleanup framework.
- Do not use plan modes, generated command assertions, private PostgreSQL/Redis/
  object-store/filesystem inspection, fabricated agents or telemetry, local
  resume manifests, compatibility paths, or mock success.
- A paid scenario must be unmistakably named; invoking its exact module is the
  explicit authorization. Do not add redundant live or paid-confirmation
  environment flags. It cannot directly provision infrastructure or rebuild
  local services. The production provider owner may lazily create its durable
  zero-capacity infrastructure as part of the workload lifecycle; the scenario
  must not bypass that owner. Cleanup and zero-cost proof remain independently
  callable.
- A connected-account scenario may automate the exact customer authorization
  action returned by the public product using ambient customer credentials.
  It must validate the account, provider host, immutable template, operation,
  and parameters before executing that action; it may not create provider
  capacity directly.
