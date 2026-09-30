Apps, deployments, and releases rewrite plan

Investigated at `a2c7348b1` on 2026-09-30. This is an implementation plan. The first three items in `update.md` are complete; this item remains open.

The rewrite should publish complete deployments, recover interrupted lifecycle operations, and fetch only the workloads needed by each read. Preserve the public deployment model, supported workflows, and tenant boundaries.

What the investigation found

- `control/deployments.py` publishes a deployment row before registration, app binding, and schedule creation finish. Failure handling spans several transactions and compensating writes. Function warm floors are released before registration succeeds, so preserving the old active flag alone does not preserve the previous version's capacity.
- `control/deployment_registration.py` repeats app resolution and converts already typed configuration through JSON and dictionaries. Registration has its own cleanup path, separate from deployment cleanup.
- Deployment start, stop, scale, and execution cleanup decisions also live in `operations/management.py`. App lifecycle and prune have additional mutation paths. Those callers need one deployment owner and consistent concurrency rules.
- `DeploymentResourceService.resolve_target_in_session` fetches matching versions and chooses one in Python. Management pagination loads the entire result before slicing. Summaries fetch historical configurations to calculate current workload facts.
- `ManagementService.latest_deployments` groups by kind and name without app identity. Two apps with the same workload name can be collapsed into one result.
- App lifecycle already has durable revisions, claims, publication state, and shutdown intents. Preserve those guarantees while simplifying the implementation. Its reconciler currently claims up to 25 apps before processing them sequentially with a 30-second claim timeout.
- Prune already has durable operation identity, snapshot validation, exact deployment targets, and restart recovery. Reuse those ideas and shared cleanup behavior instead of introducing a competing lifecycle framework.
- `control/releases.py` is a small platform release reader and worker compatibility gate. It is distinct from user workload versions. Fleet rollout belongs to compute and was covered by the earlier checklist item.

Measured baseline

Local PostgreSQL and Redis, one function workload, 1/10/100 retained versions, five calls per measurement. Query counts include statements observed through SQLAlchemy. Bytes are the sum of returned PostgreSQL field values, excluding protocol overhead. Timings are local means, not production latency estimates.

| Operation | Versions | Statements | Rows returned | Bytes returned | Mean ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| Resolve one deployment | 1 | 3 | 2 | 3,737 | 2.83 |
| Resolve one deployment | 100 | 3 | 101 | 338,211 | 40.75 |
| List latest deployments | 1 | 3 | 2 | 3,737 | 2.27 |
| List latest deployments | 100 | 3 | 101 | 338,211 | 22.96 |
| App summaries | 1 | 11 | 5 | 4,730 | 7.22 |
| App summaries | 100 | 11 | 104 | 339,206 | 22.84 |
| Idle app reconciliation | 100 | 2 | 0 | 0 | 0.71 |

The existing deployment, version-history, pruning, app-lifecycle, and release-admission suites passed all 28 tests at the investigated revision:

```sh
uv run --group dev pytest -x -q --tb=short \
  packages/control/tests/test_deployment_services.py \
  packages/control/tests/test_deployment_version_history.py \
  packages/control/tests/test_deployment_pruning.py \
  packages/control/tests/test_app_lifecycle.py \
  packages/control/tests/test_release_admission.py
```

The measurements used `isolated_services` and repeated deployment of a function named `measure` with handler `pkg:run` in the default workspace. After creating each version count, five calls each exercised `deployment_resources.resolve_target`, `ManagementService.latest_deployments`, `ManagementService.app_summaries`, and `apps.reconcile_pending`. An SQLAlchemy `after_cursor_execute` listener counted statements, result rows, and field-value bytes; timing covered each group of five calls. The temporary measurement test was removed after investigation. Recreate and retain a reproducible benchmark before implementing the rewrite.

These results establish a starting point. Concurrent write costs, representative multi-app loads, and deployed query rates still need measurement.

Implementation sequence

1. Establish the behavior and measurement boundaries.

   Record current API/SDK/CLI behavior for version selection, failed deployments, stopped latest versions, paused apps, scaling, domains, pruning, and source-stub reuse. Extend the measurements to multiple tenants/apps, large retained histories, concurrent deployment and lifecycle requests, and active recovery. Count statements, returned bytes, lock waits, and elapsed time. Freeze the affected production-code baseline and count all added code and migrations in the final comparison.

2. Rewrite deployment publication as one domain transaction.

   Make `control.DeploymentService` own app resolution, version allocation, normalized workload configuration, deployment/stub binding, schedules, and supersession. Normalize configuration once using typed models. Use the app-name lock for first creation and the app row for later publication/lifecycle conflicts; retain database uniqueness constraints.

   Resolve placement and prepare required provider resources outside the publication transaction, then revalidate the relevant state before committing. Persist recovery intent before provider preparation so a process crash cannot strand resources. Commit deployment, stub, app binding, schedule, warm-floor changes, supersession, and durable follow-up intent together. Keep external calls outside those locks. Failed publication must leave the previous workload usable.

   Remove the separate registrar and its compensating registration cleanup. Preserve externally observable failed-version/history semantics unless an intentional change is explicitly agreed.

3. Give deployment mutations and recovery one owner.

   Route direct start/stop/delete/scale, app lifecycle, and prune through common deployment transition rules in control. Keep cross-service cleanup coordination in operations and process composition in apps. Container shutdown, task/Redis cleanup, provider reconciliation, and notifications remain with their responsible services, called through explicit dependencies.

   Record the exact affected deployment and container identities before committing a stop or delete. A later retry must never discover and stop replacement containers. Persist follow-up work and stable event identities with the state change, then execute it after commit and retry it after restart.

   Share the cleanup implementation with existing app/prune recovery. Add only the durable state ordinary deployment operations are missing; avoid a generic job engine or duplicated history store. Preserve prune request identity and snapshot validation. Use a chained migration with explicit DDL and safe handling of existing pending work.

4. Simplify app lifecycle around its existing guarantees.

   Keep public states and operation revisions. Pause captures only the deployments it stopped; resume restores that set and leaves explicitly stopped deployments stopped. Delete blocks new execution before cleanup. Select IDs and lifecycle facts directly in SQL, batch related schedule reads, and remove repeated full-record loads. Claim only work that can begin within its lease, with a bounded reconciliation budget. Check both revision and claim ownership before mutation so an expired claimant cannot continue after another worker takes over. Preserve deterministic publication and recovery from worker disconnects.

5. Replace history-loading reads with scoped SQL queries.

   Resolve an exact or latest version using ordering and `LIMIT 1`. Apply cursor predicates and `LIMIT + 1` in SQL while preserving the existing cursor contract. Select latest workloads by workspace, app, kind, and name. Aggregate summary counts in SQL and fetch configurations only for the current workloads and displayed latest deployment. Keep the rule that invoking a stopped latest version fails instead of falling back to an older version. Split the oversized repository by responsibility as these queries are rewritten, updating imports directly without compatibility wrappers.

6. Tighten release composition and update every caller.

   Keep platform release state and worker compatibility in their existing ownership boundaries. Supply the release reader through composition and reuse one release snapshot per reconciliation pass where appropriate. Preserve missing-release admission behavior, runtime image/hash checks, and generation handling. Do not invent an app-release table or reopen compute rollout. Update API, scheduler, execution, devbox, gateway, and management callers together. Move deployment-related schedule integration into its owner without expanding this item into the later cron rewrite.

Acceptance and completion

- Prove atomic failed publication, concurrent first deployment/version allocation, deployment versus pause/delete races, and single-active devbox disk ownership with real PostgreSQL.
- Prove crash recovery after commit, exact shutdown targets after a later restart/redeployment, notification retries, prune snapshot conflicts, and sibling tenant/app preservation with the real relevant services.
- Exercise deployment, invocation, scale, stop/start, app pause/resume/delete, and prune through the public API/SDK. Preserve configuration defaults and omitted-versus-zero behavior. Use a real container/provider for any changed boundary; record unavailable credentials as an acceptance gap.
- Verify the migration against metadata, upgrade from the current revision with existing lifecycle data, and focused typing/lint checks. Replace implementation-shaped tests with unique outcome/concurrency/recovery evidence.
- Repeat the baseline with idle, active, historical, and concurrent loads. At 100 versions, target over 90% fewer returned bytes for single-target/latest reads. Pages must fetch only a page plus one; summaries must return data proportional to current workloads. Inspect query plans as well as returned bytes so history scans are not merely hidden inside SQL.
- Aim for at least a 20% reduction in the rewritten workflow code and a substantial net production-code reduction across the whole change, including new recovery code and migrations. File moves and deleted tests do not count as production simplification. Reconsider the design if added recovery machinery erases the reduction.
- Deliver one coherent change with the old paths removed. After an authorized deployment, verify query rates at the real replica count and reconciliation cadence. Mark the checklist item complete only after the required acceptance and measurements are recorded.

Recommended scope decisions: keep public contracts stable; keep individual workload deployment and existing app/prune semantics; replace the internal workflows freely. A new all-or-nothing multi-workload release product would be a separate contract decision.
