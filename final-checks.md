# CLI and SDK user checks

**121 of 122 checks accepted. One production capacity-reporting check remains.**

120 checks have production workflow evidence. SDK-68 was accepted on focused
owner evidence with the owner's approval; natural artifact expiry was not
observed. The print-only join check was removed from scope, not counted as a pass.
Accepted checks are removed from this list.

Production is on **0.0.100**, generation **31**, from
[PR #288](https://github.com/AmbientWare/lazycloud/pull/288), merged at `73a625863`.
[Ship 35011652434](https://github.com/AmbientWare/lazycloud/actions/runs/35011652434)
published the SDK, platform images, agent, and worker. Argo is synced and healthy
at `61b8a22d`; migration `0048_capacity_progress` completed. Both managed workers
are admitted at generation 31.

## CLI

No unresolved checks from the original checklist.

## SDK

- [ ] SDK-41 Report provider capacity failures accurately during GPU retries.
  **PARTIAL.** A production T4 function returned the expected GPU result.
  A separate Spot attempt encountered recorded AWS shortages and a quota
  rejection while public progress showed generic starting/queued messages.
  PR #288 distinguishes quota rejections and retains the latest capacity failure
  through retries until worker assignment. Migration 0048 indexes that lookup.
  Local capacity/progress acceptance passed, but the production 0.0.100 retest
  failed. Task `08bb4dfb-4577-48c6-90d4-3fd51138037f` requested one T4 on Spot.
  AWS rejected g4dn.2xlarge launches in us-east-1f twice for insufficient capacity.
  Public SDK progress still said "Starting compute for this function" through
  19:44:34 UTC on September 15. The capacity operation remained `requested` with
  no failure code despite the scheduler reporting `provider_acquisition_rejected`.
  The diagnosis is not reaching the operation record consumed by public progress.
  The task was cancelled and its container stopped. The test capacity group has
  zero desired instances and no instances; the two platform workers remain.
  Follow-up fix records provider-neutral failure codes on affected acquisition
  operations in the same transaction as pool reconciliation, before launches
  stop. The production ordering fails on the old code and passes with the fix.
  Local compute, request reassignment, cleanup, and progress checks pass.
  The user request stays pending while acquisition attempts retry, until capacity
  arrives, cancellation, or the capacity-wait timeout. The current scheduler
  capacity-wait limit is 15 minutes, separate from workload execution timeout.
  Production verification remains open until this follow-up is deployed.

## Additional work in PR #288

Local acceptance passed using ordinary CLI commands and public SDK calls against
the local Compose stack, account/workspace mclean-connor.

- Function serve: invocation from another terminal, concurrent call draining,
  source reload in the same container, import-error reporting and recovery.
- Endpoint and ASGI serve: public requests, ASGI source reload, and Ctrl+C cleanup.
- Typed app export: sync/async calls for functions, endpoints, and ASGI; default
  and custom OpenAPI discovery; supplied JSON schema; raw HTTP without a schema;
  HTTP errors and empty 204 responses.
- Generated route methods also called real FastAPI, Litestar, and Quart-Schema
  apps. Django Ninja has not been exercised.
- ASGI response cleanup: repeated requests complete their durable tasks and
  release dispatch capacity even when the caller closes after receiving the body.

Pods and sandboxes are excluded from serve/export scope. This added work does
not change the original 121/122 count.

Production verification passed using published `lazycloud-client==0.0.100` and
workspace mclean-connor. The restored landing section appears below compute.
Typed export discovered the ASGI schema; function, endpoint, ASGI, and async ASGI
calls returned the expected values. Their durable tasks completed. Function serve
returned value 100 and stopped cleanly with Ctrl+C.

Focused runner, scheduler, API, SDK, and capacity checks passed. The two stale
test fixtures found by CI were updated; all eight affected API tests and local
web type checking pass. See the PR for current CI results.

Cleanup: local acceptance app `db87362b-9dc1-4088-bedb-ee469309f0e9` is deleted.
Its 12 deployment containers are stopped/exited, and all serve previews are
stopped. The two standalone framework servers and test database services are
stopped. The main local stack remains healthy.

Production cleanup: acceptance app `d40503b8-2abb-48c0-a20e-9fd1302e1f26` is
deleted, with no active deployments. All five execution containers, including
the function preview, are stopped. User workloads were preserved.

## Working rules

- Use account mclean-connor. Record the target and client version.
- Test ordinary CLI commands and short public SDK examples with intended
  workloads. Verify prerequisites and distinguish startup/capacity delays from
  execution failures and incorrect output. PARTIAL is not PASS.
- Batch related fixes and refresh affected consumers together. Commit each fix
  separately. Reuse healthy infrastructure and run narrow checks per completed
  slice; do not rerun accepted checks as routine feature validation.
- Record actual responses and scoped cleanup. Preserve user workloads.
- Apply unslop. Keep unresolved checks here and remove them only after production
  acceptance or explicitly approved owner evidence.
- Do not deploy another change without approval.
