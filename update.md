Goal: Fully rewrite the services to make LazyCloud simpler, faster and more scalable, with substantial net code reduction and measurable performance improvements while preserving the same underlying functionality.

Refactor rules:

- Treat each service as a full rewrite. Reconsider responsibilities, state, workflows, queries, dependencies and composition together; existing implementations are not constraints.
- Preserve supported workflows, public contracts, security, durability, concurrency, failure handling, recovery and cleanup. Removing capabilities or weakening guarantees is not optimization; intentional behavior changes require owner direction.
- Reduce total complexity and production code. Delete redundant logic, unnecessary abstractions and superseded implementations. Small patches, file moves, cosmetic cleanup and added layers alone do not meet the goal.
- Separate cross-service coordination from domain decisions. Keep each responsibility with its canonical owner and dependencies explicit.
- Refactor tests alongside production code. Rewrite and simplify useful tests; remove obsolete cases, duplicate coverage and unnecessary fixtures/helpers/frameworks. Preserve unique proof of material behavior and guarantees without preserving the old implementation's shape.
- Measure code reduction and performance before and after each rewrite, including database work with representative history, idle load and concurrent activity. Passing tests alone does not prove optimization.
- Finish the service's coherent change and required acceptance before marking it complete. Keep refactor process rules in this file; AGENTS.md files contain only permanent repository standards.

Services:

- [ ] Scheduling and autoscaling
- [ ] Compute capacity and fleet management
- [ ] Control plane
- [ ] Apps, deployments, and releases
- [ ] Cron jobs
- [ ] Container lifecycle
- [ ] Task lifecycle
- [ ] Function execution
- [ ] Endpoint execution
- [ ] Pods and devboxes
- [ ] Shells and SSH
- [ ] Image builds and distribution
- [ ] Worker runtime
- [ ] Worker repository and credentials
- [ ] Agents and machine lifecycle
- [ ] Gateway, routing, and tunnels
- [ ] Authentication and authorization
- [ ] Users, workspaces, and invitations
- [ ] Billing and payments
- [ ] Usage and metering
- [ ] Object storage and artifacts
- [ ] Volumes and disks
- [ ] Checkpoints, retention, and cleanup
- [ ] Cache and source cache
- [ ] Maps and queues
- [ ] Secrets
- [ ] Custom domains
- [ ] Events, logs, and metrics
- [ ] Notifications
- [ ] Operations and administration
