# CLI and SDK user checks

PR #282 shipped as production 0.0.95. Shipping a fix does not count as passing.

**111 of 122 current checks passed; 11 remain unresolved below.**
Passed checks have been removed. Of those passes, 90 retain 0.0.93 evidence,
18 were verified on 0.0.94, and 3 were verified on 0.0.95.
Earlier passes retain their prior-release evidence.
The print-only join check was removed from scope, not counted as a pass.

Released fixes in 0.0.95: Compute settings inventory during
workspace deletion, empty joined-pool cleanup, and removal of `machine join --print-only`.
Pool cleanup preserves pending joins and offline machines, excludes platform/AWS
capacity, and rejects work targeting a removed pool. Seven focused local owner
checks pass. Production acceptance used published client 0.0.95.
Argo is healthy and synced at deployment ca648a61, release generation 26.

Removed CLI-44, CLI-48 and SDK-40 after production acceptance and cleanup.
The disposable host was removed and every test container is terminal.

PR #283 is merged into main at `7175e0d2`, not released:
forward CLI options into the installer and send SIGTERM when stopping the agent.
The join command parses with the options under POSIX sh. On the actual joined
host, the supervisor fix reduced restart from a 30-second forced kill to a clean
0.35-second restart; a subsequent invocation returned the expected result.
The shipped supervisor was restored before production removal acceptance.

Remaining known problems: warm-function preparation race, intermittent endpoint
dispatch delay, unavailable production TCP ingress, and Docker image-cache publication.
Docker registry and Compose checks are blocked by the image-cache failure.
The current batch is on `fix/sdk-failure-batch`, based on that merge.
Keep speed work limited to the unresolved endpoint checks.

Local progress, 2026-09-15:
- Restored the local agent after reboot. Docker had recreated its missing worker
  configuration file as an empty directory. The worker is healthy again.
- Committed provider launch error details, agent retry diagnostics, correction of
  invented container start times, and cancellation handling during startup.
- Twenty worker checks and the focused API/compute checks passed. Changed
  production Python files pass Ruff and type checking.
- A public function returned 2. Warm calls reused one container and PID 1.
- Docker ran Alpine and returned `docker-ok`. A fresh Docker-enabled image with
  a 256 MiB layer built and published. This does not resolve the production PUT
  failure; the image-cache error now retains its transport cause.
- Compose returned `compose-ok`; its containers and volume were removed and the
  sandbox terminated.
- All 11 local endpoint responses matched. Five concurrent calls took
  2.15–2.21 seconds; five later warm calls took 0.49–0.65 seconds.
- The API and worker are being refreshed together for validation of the startup
  cancellation change. No additional production passes are claimed.

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

- [ ] CLI-46 Join a disposable machine in foreground mode and run work on it. PARTIAL, 0.0.95: appending `--foreground` or resource options after the generated command's final `fi` causes a shell syntax error before installation. Fix committed; ordinary foreground join still needs a released retest.
- [ ] CLI-47 Join a disposable machine in service mode and run work after a service restart. PARTIAL, 0.0.95: default service join and workloads succeeded. Restart required systemd to kill the process after 30 seconds because the supervisor sent SIGINT to a child that ignores it. The SIGTERM fix passed on the test host, but remains unreleased.

## SDK

Use public imports and small examples, one behavior at a time.

- [ ] SDK-23 Set a warm replica count and verify reuse and cleanup after deletion. PARTIAL, 0.0.94: calls reused container dd7680c5 and PID 1; deletion stopped it. Preparatory container 2a5ad142 still hit a terminal container cannot restart during mark-running. Startup race remains.
- [ ] SDK-31 Send concurrent endpoint requests and inspect every response and elapsed time. PARTIAL, 0.0.94: five concurrent requests returned exact HTTP 200 responses in 11.33–13.25 seconds, but a later warm request took 27.45 seconds. Latest eight warm repeats passed in 1.01–1.69 seconds. Intermittent delay remains; speed investigation paused at the owner's request.
- [ ] SDK-32 Request a starting or stopped endpoint and inspect the actual failure or recovery. PARTIAL, 0.0.94: stopped deployment now returns a clear 503 and start restores service. Intermittent warm routing delay remains with SDK-31; investigation paused at the owner's request.
- [ ] SDK-41 Run a GPU workload on matching hardware; check invalid or unavailable GPU requests. PARTIAL, 0.0.94: GPU execution previously passed on an actual T4. Retest task 767b5e2e never reached a worker: repeated acquisitions ended with provider_launch_failed, while public Task.pending_progress kept saying "Starting compute for this function." No provider error detail was retained in the sampled operations. Cancelled the task; its container is stopped and all its acquisition operations are released. This is an acquisition/reporting failure, not a workload execution failure.
- [ ] SDK-44 Exchange data through an exposed TCP port where ingress is configured. PARTIAL, 0.0.94: deploy correctly reports TCP ingress is not configured on this installation. Production TCP exchange remains unavailable; local TCP passed.
- [ ] SDK-58 Build and run a Docker container inside a sandbox; inspect logs and stop it. PARTIAL, 0.0.95: build fdd988bf failed after 60.1 seconds with "image layer cache population failed: content cache unavailable". Its execution container is terminal and execution cleanup is complete. No Docker sandbox was created. SDK-59 and SDK-60 remain blocked by this build failure.
- [ ] SDK-59 Push and pull a test image through an authorized registry and remove test objects. BLOCKED, 0.0.94: SDK-58 prevents creating the Docker sandbox. No registry objects created.
- [ ] SDK-60 Run a small Docker Compose app inside a sandbox and remove its containers and volumes. BLOCKED, 0.0.94: SDK-58 prevents creating the Docker sandbox. No nested containers or volumes created.
- [ ] SDK-68 Observe natural artifact retention expiry before claiming retention works. BLOCKED by the actual retention window. This account's newly uploaded artifact expires on 2026-12-13. No clock/row edits or shortened policy used; metadata alone does not prove natural expiry. Signed-URL expiry is separate in SDK-67.
