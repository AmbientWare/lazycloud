# CLI and SDK user checks

PR #281 shipped as production 0.0.94. Shipping a fix does not count as passing.

**108 of 122 current checks passed; 14 remain unresolved below.**
Passed checks have been removed. Of those passes, 90 retain 0.0.93 evidence and
18 were verified on 0.0.94. This is not a full rerun on 0.0.94.
The print-only join check was removed from scope, not counted as a pass.

Unreleased fixes on `fix/remaining-user-checks`: Compute settings inventory during
workspace deletion, empty joined-pool cleanup, and removal of `machine join --print-only`.
Pool cleanup preserves pending joins and offline machines, excludes platform/AWS
capacity, and rejects work targeting a removed pool. Seven focused local owner
checks pass; joined-host production acceptance remains open.

Remaining known problems: warm-function preparation race, intermittent endpoint
dispatch delay, unavailable production TCP ingress, and Docker image-cache publication.
Docker registry and Compose checks are blocked by the image-cache failure.
The owner paused database and speed investigation; continue the remaining checklist.

Rules:
- Use account mclean-connor. Record target and published client version.
- Test ordinary CLI commands and short public SDK examples with intended workloads.
  Internal clients may diagnose problems; they do not count as user acceptance.
- Verify prerequisites. Separate cold startup, warm execution, capacity shortages,
  incorrect output and actual execution failures.
- Record the actual response, time and cleanup. PARTIAL is not PASS.
- Complete the remaining first-pass checks, batch related fixes, commit each fix
  separately, verify affected workflows, then perform final validation.
- Finish related changes before refreshing all affected package consumers together.
  Reuse healthy infrastructure. Use narrow checks while editing; reserve the broad
  validation pass for the completed batch.
- Apply unslop. Keep only unresolved checks here. Remove a check after its
  production outcome and scoped cleanup pass; do not remove partial results.
- Preserve user workloads. Clean up only resources created for these checks.
- Do not deploy another change without approval.

## CLI

Commands below start with `lazycloud`.

- [ ] CLI-44 Use `machine list` and match its entries to machines owned by the account. PARTIAL: empty inventory returned in 0.90 seconds. Joined-machine visibility awaits CLI-46; machine ownership spans the account's workspaces.
- [ ] CLI-46 Join a disposable machine in foreground mode and run work on it. BLOCKED: current host UID 1000; `sudo -n -l` requires a password. No privileged disposable host is prepared. No install attempted.
- [ ] CLI-47 Join a disposable machine in service mode and run work after a service restart. BLOCKED: same host-privilege prerequisite as CLI-46.
- [ ] CLI-48 Remove the test machine and verify host cleanup and stopped scheduling. BLOCKED: no machine joined in this pass. Do not remove platform or pre-existing machines.

## SDK

Use public imports and small examples, one behavior at a time.

- [ ] SDK-23 Set a warm replica count and verify reuse and cleanup after deletion. PARTIAL, 0.0.94: calls reused container dd7680c5 and PID 1; deletion stopped it. Preparatory container 2a5ad142 still hit a terminal container cannot restart during mark-running. Startup race remains.
- [ ] SDK-31 Send concurrent endpoint requests and inspect every response and elapsed time. PARTIAL, 0.0.94: five concurrent requests returned exact HTTP 200 responses in 11.33–13.25 seconds, but a later warm request took 27.45 seconds. Latest eight warm repeats passed in 1.01–1.69 seconds. Intermittent delay remains; speed investigation paused at the owner's request.
- [ ] SDK-32 Request a starting or stopped endpoint and inspect the actual failure or recovery. PARTIAL, 0.0.94: stopped deployment now returns a clear 503 and start restores service. Intermittent warm routing delay remains with SDK-31; investigation paused at the owner's request.
- [ ] SDK-40 Run with explicit CPU, memory, environment and pool settings and inspect them inside the workload. IN PROGRESS, 0.0.94: task e48feadb uses cpu=0.5, memory=256Mi, pool=lazycloud and SDK_MARKER=configured. Host enforcement evidence pending.
- [ ] SDK-41 Run a GPU workload on matching hardware; check invalid or unavailable GPU requests. PARTIAL, 0.0.94: GPU execution previously passed on an actual T4. Retest task 767b5e2e never reached a worker: repeated acquisitions ended with provider_launch_failed, while public Task.pending_progress kept saying "Starting compute for this function." No provider error detail was retained in the sampled operations. Cancelled the task; its container is stopped and all its acquisition operations are released. This is an acquisition/reporting failure, not a workload execution failure.
- [ ] SDK-44 Exchange data through an exposed TCP port where ingress is configured. PARTIAL, 0.0.94: deploy correctly reports TCP ingress is not configured on this installation. Production TCP exchange remains unavailable; local TCP passed.
- [ ] SDK-58 Build and run a Docker container inside a sandbox; inspect logs and stop it. PARTIAL, 0.0.94: retest build 3904924b failed after 62.3 seconds while publishing image layers to the cache. The failed request was PUT; the client discards the transport error, so the cause remains unproven. No Docker sandbox was created. SDK-59 and SDK-60 remain blocked by this build failure.
- [ ] SDK-59 Push and pull a test image through an authorized registry and remove test objects. BLOCKED, 0.0.94: SDK-58 prevents creating the Docker sandbox. No registry objects created.
- [ ] SDK-60 Run a small Docker Compose app inside a sandbox and remove its containers and volumes. BLOCKED, 0.0.94: SDK-58 prevents creating the Docker sandbox. No nested containers or volumes created.
- [ ] SDK-68 Observe natural artifact retention expiry before claiming retention works. BLOCKED by the actual retention window. This account's newly uploaded artifact expires on 2026-12-13. No clock/row edits or shortened policy used; metadata alone does not prove natural expiry. Signed-URL expiry is separate in SDK-67.
