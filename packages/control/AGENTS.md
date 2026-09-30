# Control

- Own workspace, app, deployment, route, schedule and concurrency workflows.
  Use scoped repositories, narrow collaborators and typed domain errors.
- Carry tenant/owner scope into every repository lookup, mutation and registration.
  Public payloads belong in shared HTTP contracts.
- Stubs describe workloads without placement. Deployments pin placement at deploy;
  execution resolves other requests at start time.
- Workspace creation enforces admission before writing. Adopting an existing
  workspace is not creation; app creation has no plan-count gate.
