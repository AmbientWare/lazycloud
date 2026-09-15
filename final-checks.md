# CLI and SDK user checks

**121 of 122 checks accepted. One production capacity-reporting check remains.**

120 checks have production workflow evidence. SDK-68 was accepted on focused
owner evidence with the owner's approval; natural artifact expiry was not
observed. The print-only join check was removed from scope, not counted as a pass.
Accepted checks are removed from this list.

Production is on **0.0.99**, generation **30**, from
[PR #287](https://github.com/AmbientWare/lazycloud/pull/287).
[PR #288](https://github.com/AmbientWare/lazycloud/pull/288) contains the pending
fixes and the additional serve/export work. It has not been deployed.

## CLI

No unresolved checks from the original checklist.

## SDK

- [ ] SDK-41 Report provider capacity failures accurately during GPU retries.
  **PARTIAL.** A production T4 function returned the expected GPU result.
  A separate Spot attempt encountered recorded AWS shortages and a quota
  rejection while public progress showed generic starting/queued messages.
  PR #288 distinguishes quota rejections and retains the latest capacity failure
  through retries until worker assignment. Migration 0048 indexes that lookup.
  Local capacity/progress acceptance passed. A real provider rejection must still
  be checked through the public client after an approved deployment.

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

Focused runner, scheduler, API, SDK, and capacity checks passed. The two stale
test fixtures found by CI were updated; all eight affected API tests and local
web type checking pass. See the PR for current CI results.

Cleanup: local acceptance app `db87362b-9dc1-4088-bedb-ee469309f0e9` is deleted.
Its 12 deployment containers are stopped/exited, and all serve previews are
stopped. The two standalone framework servers and test database services are
stopped. The main local stack remains healthy. Production was not changed.

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
