Goal: Make LazyCloud fast, simple to understand and operate, and efficient as usage grows. Rewrite each assigned area to improve real user workflows, remove unnecessary work and complexity, and substantially reduce production code while preserving supported functionality and guarantees.

This file is a reusable prompt for an agent assigned one checklist item. The checklist only tracks the separate areas of work; it is not an implementation plan. Investigate the assigned area and choose the changes from evidence in the code and running system.

Agent instructions:

- Treat the assigned area as a full rewrite opportunity. Reconsider its architecture, responsibilities, data flow, state, algorithms, dependencies and process lifetimes. Existing classes, service boundaries and sequences of operations are not requirements. Choose the simplest design that meets the actual needs; a large rewrite is appropriate when the problems are structural.
- Start with what the user experiences. Trace the relevant workflows through their callers, services, queues, workers, containers and providers. Find where time and resources go, what work repeats, and which steps are unnecessary. Investigate suspected causes rather than assuming that the database or any other component is the bottleneck.
- Improve behavior and execution together. Look for better ways to start, serve, schedule, coordinate, retry, recover and clean up. Remove unnecessary steps and states, shorten slow paths, fix incorrect behavior, and make failures easier to understand and recover from. Query tuning and local cleanup alone do not satisfy the task when larger workflow or design problems remain.
- Examine startup and request costs wherever relevant. Examples include agent readiness, container startup, cold and warm HTTP requests, routing and task dispatch. Investigate repeated initialization, imports, process creation, serialization, network hops, blocking work, polling delays, lock contention and redundant validation or reads. Remove unnecessary work before adding caches, concurrency or background jobs; any added mechanism must justify its complexity and preserve correctness.
- Repair ownership and service boundaries. Give each decision, configuration and piece of state one responsible owner. Remove forwarding layers, duplicate orchestration, circular dependencies and abstractions without a concrete purpose. Keep domain decisions separate from cross-service coordination, persistence and process composition. Update callers and dependencies together instead of preserving broken boundaries through wrappers.
- Keep the assigned checklist item as one task, but follow its problems across package and service boundaries. Include the related changes needed to make its workflows coherent. Do not limit the rewrite to a single file or package, and do not expand it into independent rewrites of the other checklist items.
- Preserve supported workflows, public contracts, authorization, tenant isolation, data integrity, durability, concurrency, failure recovery and cleanup. Preserve the guarantees, not the machinery currently used to provide them. Removing capabilities, hiding errors or weakening guarantees is not optimization; intentional product or public-contract changes require owner direction.
- Reduce total complexity and production code across the complete change. Delete superseded implementations, redundant logic, unnecessary abstractions and dead paths. Avoid replacing one complicated system with another framework. File moves, smaller files and fewer lines in one package do not count as simplification if the same work or more complexity appears elsewhere. Split oversized modules by responsibility while simplifying the underlying design.
- Refactor tests with the implementation. Keep unique proof of material user outcomes and production guarantees. Replace tests tied to the old implementation's shape, and remove duplicate coverage and unnecessary fixtures or test frameworks. Exercise real service, container and provider boundaries when they change; missing access is an acceptance gap, not a reason to substitute fake success.
- Establish a reproducible baseline before changing the relevant behavior, then measure the same workloads afterward. Choose metrics that reflect the problem: readiness and startup time, cold and warm latency, throughput, CPU, memory, network work, database statements and returned bytes, contention, or recovery time. Use representative idle, active, concurrent and historical loads as applicable. Report sample counts and latency distributions for timing claims, and keep workload, resources and cache conditions comparable.
- Verify the improvement across the complete affected workflow. A faster helper is insufficient if user-visible latency is unchanged or the cost moved to another service, startup phase, background loop or failure path. Measure production-code reduction across all affected owners, including new supporting code. Passing tests alone does not establish performance or simplification. State remaining bottlenecks, tradeoffs and unverified claims plainly.
- Unless the user asks only for investigation or planning, carry the assigned task through implementation, removal of obsolete paths, focused validation and before/after evidence. Do not substitute a plan or documentation change for the rewrite. Mark an item complete only after its coherent implementation and required acceptance are finished. Keep these reusable instructions and the tracking checklist in this file; feature plans and progress narratives do not belong here, and AGENTS.md files contain only permanent repository standards.

Services:

- [x] Scheduling and autoscaling
- [x] Compute capacity and fleet management
- [x] Control plane
- [x] Apps, deployments, and releases
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
