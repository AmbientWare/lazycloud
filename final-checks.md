# CLI and SDK user checks

**118 of 122 checks accepted; 4 remain unresolved below.**
117 have production workflow evidence. SDK-68 was accepted on focused owner evidence
with the owner's approval; natural expiry of the production artifact was not observed.
Accepted checks have been removed. The print-only join check was removed from scope,
not counted as a pass.

Production **0.0.97** is active at generation **28**. Argo is healthy and synced at
`526773aa`. PR #285 merged at `2416135e` after all five checks passed.
[Ship run 34925939579](https://github.com/AmbientWare/lazycloud/actions/runs/34925939579)
published the client, agent, worker, and platform images and selected the release.

Acceptance used published `lazycloud-client==0.0.97` against https://lazycloud.dev,
account/workspace mclean-connor. Only affected unresolved workflows ran.

Removed after production acceptance and cleanup:
- CLI-46: plain `machine join` ran in the foreground with no service installed.
  The machine advertised the requested 1 CPU and 2 GiB. Its function returned
  value 97 in 11.521 seconds. Ctrl+C exited with code 0 and no traceback.
- SDK-59: authenticated registry login, push, removal of the local tag, pull, and
  execution passed. The pulled image returned `registry-ok`. The registry
  container, anonymous data volume, and temporary credentials were removed.
- SDK-60: Compose started the app and logs contained `compose-ok`.
  `compose_down(volumes=True)` removed its container and named volume.
  Docker subsequently listed no containers or volumes.

SDK-58 improved but remains partial. The fresh image build with a 256 MiB layer
completed and published, proving the cache-upload fix in production. The first
sandbox then failed at runtime startup. A retry on the same worker became ready
in 5.656 seconds. Docker image build, execution, logs, stop, and removal worked
there. A successful retry does not explain or erase the first failure.

Cleanup: sandbox `a037358a-e4c0-40dc-baf9-eef94bbeb799` is stopped. The failed
sandbox and both execution containers are terminal. Image build
`90f48ed4-6d5f-48dc-910e-f4b60ba0a58c` is complete with no pending execution
cleanup. No deployed acceptance app remains. The joined machine left through
its owning host and the CLI machine list is empty. Disposable EC2 instance
`i-0ccb64817e97e8e10` is terminated; its disk, security group, AWS key pair,
and local SSH credentials are removed. User workloads were preserved.

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

No unresolved checks.

## SDK

Use public imports and small examples, one behavior at a time.

- [ ] SDK-31 Send concurrent endpoint requests and inspect every response and elapsed time. PARTIAL: the earlier 27.45-second warm delay has not been reproduced or explained. On 0.0.96, all 11 responses matched: cold 7.443 seconds, five concurrent calls 2.387–3.444 seconds, five warm calls 1.318–1.771 seconds. This successful sample does not establish that the intermittent delay is fixed. Not rerun in 0.0.97.
- [ ] SDK-41 Run a GPU workload on matching hardware; check invalid or unavailable GPU requests. PARTIAL: GPU execution previously passed on an actual T4. The failed 0.0.94 acquisition was traced to AWS reporting insufficient g4dn.8xlarge capacity in us-east-1d. Release 0.0.96 classifies that real AWS message as `capacity_unavailable` and retains a safe diagnosis. The classification passed against the actual AWS record and the compute owner preserved it. Public production progress during a new capacity rejection remains unverified; no new GPU request ran in this batch.
- [ ] SDK-44 Exchange data through an exposed TCP port where ingress is configured. PARTIAL: PR #286 adds a shared TCP NLB, a cert-manager wildcard certificate, and Terraform-owned DNS-only `*.tcp.lazycloud.dev`. Helm, certificate schemas, Kubernetes server dry run, and certificate reload validation passed. Existing deploy credentials can read the Cloudflare zone; they cannot manage API tokens. Production needs `LAZYCLOUD_TCP_DNS_API_TOKEN`, restricted to this zone with DNS Edit and Zone Read, in `lazycloud-prod/operator`. Keep the broad deploy token outside the cluster. After that, merge and deploy, set Terraform's target to the provisioned TCP NLB, review/apply the DNS plan, and test real TLS connections, workload isolation/deletion, certificate renewal/reload, and replica replacement. ExternalDNS is removed. No TCP infrastructure has been deployed yet.
- [ ] SDK-58 Build and run a Docker container inside a sandbox; inspect logs and stop it. PARTIAL, 0.0.97: the cache upload and image build passed. First sandbox `a15b3960-25f8-4792-9b9f-7e53455be0f0` failed with `cannot read client sync file: waiting for sandbox to start: EOF`. Retry `a037358a-e4c0-40dc-baf9-eef94bbeb799` on the same generation-28 worker passed Docker build, execution, logs, stop, and removal. Host kernel logs showed no OOM kill and the worker had not restarted. The first runtime startup failure is unexplained; do not mark it passed based on the retry.
