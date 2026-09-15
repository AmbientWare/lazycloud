# CLI and SDK user checks

**115 of 122 checks accepted; 7 remain unresolved below.**
114 have production workflow evidence. SDK-68 was accepted on focused owner evidence
with the owner's approval; natural expiry of the production artifact was not observed.
Accepted checks have been removed. Of those passes, 90 retain 0.0.93 evidence,
18 retain 0.0.94 evidence, 3 retain 0.0.95 evidence, and 3 passed on 0.0.96.
The print-only join check was removed from scope, not counted as a pass.

Production 0.0.96 is active at generation 27. Argo is healthy and synced at
`2e528ab3`. PR #284 merged at `a882c69a` after all four CI checks passed.
[Ship run 34921370649](https://github.com/AmbientWare/lazycloud/actions/runs/34921370649)
published the client, agent, worker, and platform images and selected the release.
This includes PR #283's machine-join option forwarding and supervisor SIGTERM fix.

Acceptance used published client 0.0.96 against https://lazycloud.dev,
account/workspace mclean-connor. The workers used release generation 27 and the
published 0.0.96 worker image. Only affected unresolved workflows ran.

Removed after production acceptance and cleanup:
- CLI-47: service join succeeded; restart took 0.965 seconds and the next workload
  returned the expected value.
- SDK-23: warm calls returned the expected values and reused container
  `74ce3238-9511-4092-bb62-22c1ee68b084` and PID 1. Calls took 4.03 and 0.632 seconds.
  Pause/resume and deletion left all app containers stopped with no startup errors.
- SDK-32: a paused endpoint returned a clear HTTP 503 in 0.784 seconds. Resume
  restored the expected HTTP 200 response in 4.997 seconds. The separate intermittent
  warm-delay issue remains under SDK-31.

Cleanup: deleted test app `b156849a-5871-46cd-9520-643c1701a0ba` and its two
deployments. All four app containers are stopped; both joined-host invocation
containers exited. The failed Docker build's container is terminal and execution
cleanup is complete. No Docker sandbox or registry objects were created.
The joined machine left through the owning host and no longer appears in the CLI.
EC2 instance `i-0900a583ceb005b2f` is terminated; its disk, security group, AWS key
pair, and local SSH credentials are removed.

Current fix batch, `fix/join-and-cache-failures`, not deployed:
- Foreground join is the default; `--background` explicitly installs a service.
- Saved agent identities migrate the obsolete bootstrap routing fields without
  discarding credentials or machine identity. Unknown fields still fail validation.
- SIGINT and SIGTERM use graceful shutdown; repeated signals cannot interrupt cleanup.
- Cache uploads retain their 120-second total budget instead of a two-second response
  header deadline. Reads retain a two-second budget. Chunked uploads fill bounded
  read buffers before cache admission, avoiding a capacity scan per small HTTP chunk.
- The old cache client failed the delayed-response check with the production error;
  the patched client passed. A fresh local image with a 256 MiB layer built and
  published. Docker execution, Compose, and authenticated registry push/pull passed.
  Their containers, volume, sandbox, and temporary credentials are removed.
- SDK-68 passed the four existing plan-retention cases: expiry blocks reads and URLs,
  cleanup deletes expired artifacts, and later uploads survive. No production dates
  or retention settings were changed.

Rules:
- Use account mclean-connor. Record target and published client version.
- Test ordinary CLI commands and short public SDK examples with intended workloads.
  Internal clients may diagnose problems; they do not count as user acceptance.
- Verify prerequisites. Separate cold startup, warm execution, capacity shortages,
  incorrect output and actual execution failures.
- Record the actual response, time and cleanup. PARTIAL is not PASS.
- Batch related fixes, commit each fix separately, and verify affected workflows.
  Do not rerun the previously passed checklist as routine acceptance.
- Finish related changes before refreshing all affected package consumers together.
  Reuse healthy infrastructure. Use narrow checks while editing; reserve broad
  validation for a release or an explicit broad quality claim.
- Apply unslop. Keep only unresolved checks here. Remove a check after its
  production outcome and scoped cleanup pass, or explicitly approved owner evidence
  supports acceptance. Record that distinction; do not remove partial results.
- Preserve user workloads. Clean up only resources created for these checks.
- Do not deploy another change without approval.

## CLI

Commands below start with `lazycloud`.

- [ ] CLI-46 Join a disposable machine in foreground mode and run work on it. PARTIAL, 0.0.96: `--foreground --max-cpu 1 --max-memory 2Gi --state-dir /var/lib/lazycloud-0096` parsed correctly. The machine advertised those limits and its workload returned the expected value. Ctrl+C then printed a KeyboardInterrupt traceback during tunnel shutdown and an unawaited-coroutine warning. Option forwarding is fixed; foreground shutdown still needs correction.

## SDK

Use public imports and small examples, one behavior at a time.

- [ ] SDK-31 Send concurrent endpoint requests and inspect every response and elapsed time. PARTIAL: the earlier 27.45-second warm delay has not been reproduced or explained. On 0.0.96, all 11 responses matched: cold 7.443 seconds, five concurrent calls 2.387–3.444 seconds, five warm calls 1.318–1.771 seconds. This successful sample does not establish that the intermittent delay is fixed.
- [ ] SDK-41 Run a GPU workload on matching hardware; check invalid or unavailable GPU requests. PARTIAL: GPU execution previously passed on an actual T4. The failed 0.0.94 acquisition was traced to AWS reporting insufficient g4dn.8xlarge capacity in us-east-1d. Release 0.0.96 classifies that real AWS message as `capacity_unavailable` and retains a safe diagnosis. The classification passed against the actual AWS record and the compute owner preserved it. Public production progress during a new capacity rejection remains unverified; no new GPU request ran in this batch.
- [ ] SDK-44 Exchange data through an exposed TCP port where ingress is configured. PARTIAL, 0.0.94: deploy correctly reports TCP ingress is not configured on this installation. Production TCP exchange remains unavailable; local TCP passed. No ingress change shipped in 0.0.96.
- [ ] SDK-58 Build and run a Docker container inside a sandbox; inspect logs and stop it. FAILED, 0.0.96: build `7796ffd5-1a68-4a3f-b557-bf89101626f8` failed after 58.3 seconds. The new diagnostic reports `content cache unavailable: PUT: net/http: timeout awaiting response headers`. This is a cache-upload response timeout, not unavailable compute. The execution container is terminal and execution cleanup is complete. No Docker sandbox was created.
- [ ] SDK-59 Push and pull a test image through an authorized registry and remove test objects. BLOCKED, 0.0.96: SDK-58 prevents creating the Docker sandbox. No registry objects created. The ordinary workflow passed locally before release.
- [ ] SDK-60 Run a small Docker Compose app inside a sandbox and remove its containers and volumes. BLOCKED, 0.0.96: SDK-58 prevents creating the Docker sandbox. No nested containers or volumes created. The ordinary workflow passed locally before release.
