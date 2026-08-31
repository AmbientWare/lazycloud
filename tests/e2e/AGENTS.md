# Opt-in end-to-end scenarios

Live scenarios, each proving one production capability through its public SDK,
CLI, API, or browser path. `README.md` covers running them and their
prerequisites.

Scenarios consume an already-prepared environment. They never build, deploy,
migrate, reset, or inventory the platform: a scenario that provisions what it
then tests is mostly testing its own setup.

- `local/` targets the healthy canonical root Compose stack. `external/` targets
  an explicitly authorized provider, cluster, or other external system.
- Establish that the environment is running the code under test before trusting a
  result. A stack can report every service healthy while serving images built
  from older source; a pass against stale code proves nothing, and a failure
  sends you after the wrong defect. Rebuild every image that embeds the source
  together, from one source state.
- Ordinary test discovery and changed-scope validation must not execute this
  tree. Run Python scenarios as exact modules from the repository root and
  browser scenarios as exact nodes, never by file path, and never with
  import-path bootstrap code.
- Declare exact prerequisites and target guards. Exit `77` when a prerequisite is
  unavailable, `0` only after terminal production evidence, and nonzero for any
  mutation, assertion, or cleanup failure.
- Create uniquely named resources, clean them up through their public owner, and
  verify only their public terminal state. Never delete a fixed or pre-existing
  resource.
- Keep fixtures beside their one consumer. Shared support stays limited to small
  secret-safe process and live-gate helpers; do not grow a workflow, lifecycle,
  polling, deployment, or cleanup framework here.
- Do not use plan modes, generated command assertions, private datastore or
  filesystem inspection, fabricated agents or telemetry, local resume manifests,
  compatibility paths, or mock success. Each one turns a live scenario back into
  a unit test with a much longer runtime.
- A paid scenario is named unmistakably, and invoking its exact module is the
  authorization. Do not add redundant confirmation flags on top. It may not
  provision infrastructure directly or rebuild local services; it drives the
  production owner and lets that owner create whatever it lazily creates.
  Cleanup and zero-cost proof stay independently callable.
- A connected-account scenario may automate the exact customer authorization
  action the public product returns, using ambient customer credentials, after
  validating the account, provider host, immutable template, operation, and
  parameters. It may not create provider capacity directly.
