# CLI and SDK user checks

First pass complete. Only checked PASS items count as passing.

Published 0.0.93 results: CLI 35/51 PASS, 13 PARTIAL, 3 BLOCKED;
SDK 55/72 PASS, 14 PARTIAL, 3 BLOCKED. Total 90/123 PASS.

Local follow-up has verified 26 of those 33 non-passing items. This is a forecast
of 116/123 after release, not a new production pass count or a full local rerun.
Production GPU execution also passed on On-Demand hardware; SDK-41 stays partial
until the released client reports Spot capacity failures correctly. Five joined-machine
checks and natural artifact retention remain unverified. PR #281 contains the batch
on current main. All five required PR checks passed before this evidence-only update.

Current pass: production `https://lazycloud.dev`, published client 0.0.93,
Python 3.12.11. Disposable client environment and config are under
`/tmp/lazycloud-user-checks-20260914/`. Commands below use its `venv/bin/lazycloud`.

- Use only `mclean-connor`. Record the target and client version.
- User acceptance excludes the SDK `Client` facade and CLI `client` commands.
  Use App, functions, endpoints, Pods, sandboxes and storage objects directly.
  Internal clients may support diagnostics; their success is not user acceptance.
- Run one item at a time as a user would: a real CLI command or short SDK example.
- Show the command or example, expected outcome, actual output and elapsed time.
- Check prerequisites first. Test cold starts separately from running workloads.
- Keep errors and retries visible. Diagnose failures before blaming the feature.
- Present startup/capacity problems in a readable CLI card beside the workload's
  progress, with the cause, current state and next step. Keep placement consistent;
  do not bury provider failures in repeated tracebacks or generic waiting messages.
  Preserve clean machine-readable output in JSON mode.
- Record PASS, PARTIAL, FAIL, BLOCKED or INCONCLUSIVE beside each item.
  PARTIAL means some behavior works but the output or behavior is not what we want.
  Leave it unchecked and include it in the fix list. Only complete PASS counts as passing.
- Keep local and production results separate. Production uses the published client.
- Finish the first pass, batch related fixes with one commit per fix in the same PR,
  rerun affected items, then do a final pass. Apply `unslop`. No release without approval.
- Finish related fixes before refreshing the local stack. Refresh every changed-package
  consumer together, then test the affected CLI and SDK workflows. Use narrow checks
  while editing; reserve the full validation pass for the completed batch.
- Reuse healthy infrastructure, refresh all changed-package consumers together,
  and verify cleanup of named test resources. Keep credentials private.

Custom domains and connected AWS cloud remain reserved for owner testing.
Unavailable prerequisites leave items blocked. Browser use is login setup only.

Owner-requested fix: remove the CLI `client` command group and top-level SDK
`Client` export. Keep required session/control implementation internal, and update
public docs/examples so normal use goes through the resource abstractions.
Implemented in `e215ef7e3`; not released. Include this in the same fix PR.

CLI pending cards implemented in `b0840eb70`, not released. Verified with a real
pending function through the branch CLI against production. The notice appears
beneath task progress; terminals without live redraw print each status change.
Five existing SDK output-policy cases, focused type checks and Ruff passed.
Provider status accuracy remains open in SDK-41; presentation alone does not fix it.

SDK-41 additional evidence: an invalid GPU name is rejected before submission,
with the supported GPU names in the error. This does not close the capacity gap.

Task outcome reporting fixed locally: cancellation preserves its typed status and
exits 130 with task_cancelled JSON. Handler failures show Task failed and execution
timeouts show Task timed out. Real local CLI checks passed for pending cancellation,
a deliberate handler exception and a three-second timeout. 38 focused SDK cases,
type checks and Ruff passed. CLI-16 cleanup passed on local generation 64 below.

Local fix verification, release generation 63:
- CLI-12: invalid integer input failed on attempt 1 of 4 in 1.6 seconds of task
  time. Task 0713a5f6-a670-4234-9c00-928060b8aca0. Retry fix `3cf2c25b6` verified;
  the heading still says Task failed and the runtime traceback is verbose.
- SDK-39: the deliberately broken image ran one overlay build and returned its
  exit-17 step without retrying another storage driver. Fix `38ce1108b` verified.
  The branch CLI also shows Image build failed with build-step guidance, verified
  by build db84d710-9c33-44f0-aa65-c40d315a9eb6 in 7.7 seconds.
- CLI-33: attached to running Pod 832efca9-36a2-40e7-bddf-1f03c0d3dd9e, then
  stopped it. Attach reported Not reported with nonzero exit while the worker's
  exit was unknown. A later attach reported the actual code 560. Fix `4ed69fbdb`
  verified. The Pod is stopped.

Local fix verification, release generation 64:
- CLI-16: task a20cf421-df6b-4903-b32e-1f450091fc4b stayed pending in the
  deliberately empty pool. After task cancel, container
  0c6ac78b-5f43-4002-9526-3e4bc8892f4c was stopped on the next inventory read.
  No start time or handler logs. The run returned task_cancelled JSON and exit
  130. Cleanup fix `611962a73` passed without an explicit container stop.
- CLI-17: stopped running task 6ec04dba-494a-42fc-9752-376f92255c95 after tick
  18. It reported cancelled, the CLI showed Task cancelled and exited 130,
  logs stopped at tick 18, and its container was stopped.
- SDK-16: pending.spawn() returned task f0d1dcfd-ac3c-4dfd-a9f1-80ce6660e740.
  Its handle reported pending, cancel() returned that task in stopped, and the
  next read reported cancelled with no handler output. Container
  5329c621-6d46-4749-9582-d43ef05a34a1 stopped automatically.

The eight focused function-autoscaler tests and eleven SDK output tests passed.
The first scheduler test attempt could not connect because its separate test
database was stopped. Started the test dependencies, reran successfully, then
stopped those dependencies again. Ruff and focused type checks passed.

Production counts above are unchanged. These fixes have not been released.

Current fix batch:
- CLI-22/24 and SDK-32: stopped deployments and paused apps return an unavailable
  error on generated hostnames and direct routes. The latest route cannot fall
  back to an older active version. Private resource authorization remains enforced.
- CLI-25 and SDK-33: a preview request can wait through container startup within
  its existing request deadline. The CLI prints Preview URL and Container output
  without claiming the server is ready.
- CLI-23 and SDK-26: a deployed Pod floor keeps its containers alive; an explicit
  fixed replica target drains excess idle replicas without the keep-warm delay.
  Removed documentation that incorrectly scaled an endpoint with the Pod command.
- CLI-32: terminal working directory prefers the synchronized /mnt/code directory.
  Terminal latency still needs a real interactive check.
- CLI-14: Python results carry a bounded text preview produced inside the workload.
  The CLI displays it without deserializing the saved pickle. SDK results retain
  their original Python encoding. The web contract accepts the preview field.
- CLI-12/39 and SDK-65: CLI arguments validate before submission; invalid runtime
  input has a concise log. Worker HTTP failures preserve structured server errors.
  Secret and storage startup errors have concise task messages; container logs
  retain the mount failure details.
- SDK-41: rejected or unsupported acquisition reports capacity unavailable;
  typed provider failures reach progress messages and message changes redraw.
  Successful On-Demand GPU execution is recorded below; release verification of
  capacity-failure reporting remains open.
- SDK-58: documentation names both Image.with_docker() and docker_enabled=True.

Batch evidence so far: 71 focused backend cases, 33 client/runner cases and 16
worker/API cases passed. Changed Python type checks and Ruff passed; web schema
lint and type checks passed. An initial hostname assertion used an unconfigured
test domain and returned 404; configuring the gateway domain made the intended
stopped-host request pass. No production result changed.

Resolved local checkpoint blocker: two real starts reached HTTP readiness, then failed
cache admission. Containers e7f846ed-7cc2-4b34-8661-9db8e5303e25 and
68277ecb-a5f9-478f-a278-d0aa711509ff are terminal. The host is near the cache's
90% disk-use ceiling; a 1.2 GB checkpoint exceeds it. Removed only 12 identified
unused build-cache entries created by this run, reclaiming 2.541 GB, which was
insufficient. Host free space later increased to about 530 GiB; its cause was
not verified. No older cache was deleted by this run. The cache error now names
the limiting capacity and additional bytes required. Restore evidence follows.

Local generation 65, source `03901b6f`, branch CLI/SDK:
- CLI-12: `run functions.py:add invalid 22` rejected the integer before submission
  in 0.58 seconds, exit 1. JSON mode returned `invalid_input` in 0.55 seconds.
- CLI-14: `task result 54e0f3c9-2e59-4bbf-bd37-d5261d533627` displayed 42 in
  0.61 seconds. The SDK handle also returned the original integer 42.
- CLI-22/24: stop and pause returned HTTP 503 naming the inactive deployment/app
  in 0.37/0.36 seconds; start and resume restored HTTP 200. App user_checks_web.
- CLI-23, SDK-26: sdk_web_pod held two replicas beyond its five-second keep-warm,
  then scaled to one. Pause/resume returned a working HTTP server; deletion left
  all three observed containers stopped, without failed replacement containers.
  Deployment de6a3b5d-e66b-4671-9279-f77992e1a41e.
- CLI-25: the first POST to preview e9d3ea8c-5a09-475f-80f4-6bc5074777c9 returned
  HTTP 200 in 0.53 seconds without retry. This manual request may have arrived
  after backend readiness. Ctrl-C stopped the preview.
- CLI-32: `dev pod.py:web --sync .` opened /mnt/code, read sync-note.txt and
  immediately echoed typed commands. `exit 7` propagated exit 7. Stopped its Pod.
- CLI-39: a missing USER_CHECK_VALUE secret failed before handler execution with
  a concise secret-not-found card, exit 1. Three startup containers failed before
  the task settled; preserve that observation when reviewing startup retries.
- SDK-46: original Pod 7ec6016a-2fbf-41f5-8d91-4e0872f7f026 and restored Pod
  2342aaf4-f369-431d-bb0c-3a9d28939a6a returned the same in-memory instance ID
  668d3a973a954987983ef171cd3eb502, count 1003 and PID 1. Both terminated.
  The restored stop recorded exit 1; fix 32ce1d613 still needs live verification.
- SDK-58/59/60: Docker 29.8.0 in sandbox e69f31f8-dcec-4a55-ac5d-c11edf2b81d7
  built and ran an image, returned its log marker and stopped the container.
  Push/remove/pull through a disposable registry:2 succeeded; the pulled image
  returned the same marker. Registry authentication was not tested. Compose wrote
  and read its named volume; down --volumes left no volumes. Removed the registry,
  its volume and nested containers, then terminated the sandbox.
- SDK-40: task 7fb4a967-12e4-4d8b-88f8-680caa135e98 used explicit self-hosted pool,
  cpu=0.5, memory=256Mi and the configured environment marker. Actual runtime
  cgroup had cpu.weight=20, memory.low=268435456, memory.high=1235222528 and
  memory.max=1342177280. CPU/memory requests permit bursting by production policy;
  cpu.max was 330900/100000, not a half-core hard limit.
- SDK-74: foreign sandbox 7ed9f659-c312-44ab-a583-e59c4f6909a9 ran its owner's
  command successfully. A main-workspace function was denied in 0.02 seconds with
  container not found, task 6bdee2ef. Foreign sandbox stopped. No timeout recurred.
- SDK-23: two warm calls reused fbffacc7-4979-4e43-aaab-aa362451dbf7 and PID 1;
  deletion stopped it. Final scoped startup-history review remains pending.
- SDK-31: five cold concurrent POSTs produced four HTTP 200 responses and one
  HTTP 500 from a real PostgreSQL deadlock. Admission's stub lock blocked the
  container foreign-key check while waiting on the app lock. Fix a9fea36f uses
  FOR NO KEY UPDATE and prevents raw database exceptions reaching callers.
  Focused admission checks passed; the five-request live repeat remains pending.

Refresh from `8e3296d7` included endpoint admission, authorization
ordering, restored stop reasons, and TCP address/prerequisite fixes. TCP deploy
returns a TLS address; standalone TCP create rejects before scheduling. Three
TCP route/TLS cases, changed-file types and Ruff passed. No production release.

Local generation 66, source `cb40fa9ae`, follow-up:
- SDK-31: all five cold concurrent POSTs returned HTTP 200 with their individual
  markers in 3.56–3.92 seconds, no retries or hidden exceptions.
- SDK-32: greet v2 returned its updated response. After stopping v2 by deployment
  ID, the SDK request returned HTTP 503 naming inactive v2 in 0.46 seconds;
  pinned v1 still returned its original response. Restart restored v2.
- SDK-33: first POST to SDK preview 2a2c4493-33f1-4311-91f8-82c8c12827a7 returned
  HTTP 200 in 0.48 seconds. Ctrl-C exited 0 and stopped the preview.
- SDK-46/57 stop reporting: restored Pod 8a4bf437-382a-43dd-9be7-2dd7475ad4a4
  returned the original in-memory instance ID and stopped with exit 560/User.
- SDK-23: scoped durable history for warm_identity contains only the reused
  container fbffacc7-4979-4e43-aaab-aa362451dbf7, stopped 560, no startup error.
- SDK-65: deliberately invalid credentials failed S3 mount startup before the
  handler ran. Task c339bafa-0fa2-4f2a-9c39-1fdcb7d9ed73 raised TaskOperationError
  with concise bucket/prefix/credential guidance and a pointer to container logs.
  No handler logs or external bucket was created. Deleted both test secrets.
  Three startup attempts are the scheduler's separate startup-failure threshold,
  not handler retries. Valid-prefix/read-only evidence remains the production pass.

TCP live checks exposed another defect: an idle Pod's autoscaling target was
removed, and new proxy demand did not reactivate it. Requests counted as demand
but no container was scheduled. Fixed in `b993cda9`; fourteen Pod routing and
autoscaling cases and focused types passed. On local generation 68, source
`b993cda9`, an ordinary TLS socket sent `tcp-final-user-check\n` to the deployed
Pod and received the exact bytes in 2.87 seconds from a cold start, without retry.
The connection verified the local CA and supplied the generated hostname as SNI.
SDK-44 passes locally. Deleting the Pod stopped container
cc952acd-e0e0-4d91-ab21-f28f418d8a16 with exit 560. Earlier TLS timeouts exposed
the defects fixed in this batch and remain part of this record.

PR checks found a discarded result-preview exception and a stale HTTP contract
corpus. The preview now names the exception type while preserving the saved Python
value; direct owner execution confirmed the value remains 42. Regenerated the
corpus from Pydantic; all nineteen browser contract cases passed.

Production GPU retry after owner supplied AWS quota approval:
- AWS default profile reports 128 Spot G/VT vCPUs and 384 On-Demand G/VT vCPUs
  in us-east-1. The Spot request for 384 is not the applied Spot quota.
- Task ec4f5b94-282a-4dd1-b0ec-4d7e13ccfbd7 triggered automatic T4 provisioning.
  AWS rejected g4dn.2xlarge Spot capacity in us-east-1c, then other zones. A pinned
  us-east-1a retry, a8ed8af6-e41f-40c4-bc32-3a3da4db73ef, also stayed unavailable.
  ASG launch activities show capacity failures in 1a/1b/1c/1d, not quota errors;
  the actual launch template specifies Spot. Automatic zone selection did move on.
- Cancelled both tasks and explicitly stopped their pending containers
  3e0ba1fc-db2d-4d59-a17a-404cfd2d9429 and 3f7d5011-e950-4e19-85d4-76f54b2221f9.
  Published 0.0.93 still needs the pending-container cleanup fix in this PR.
- A fresh A10G request, ace106b0-b023-40d0-a4c8-e28dc72d0288, also hit Spot
  capacity failures in 1a/1d/1f. Cancelled it and stopped its pending container
  876a2fae-cad0-4094-9e1b-e2f3369652bf.
- A fresh T4 definition with `preemptible=False` automatically provisioned
  g4dn.xlarge instance i-02b3337a785f88367 in us-east-1d. Task
  bfa3e33b-be64-442b-bb47-fecdab87e114 completed after about 166 seconds of
  provisioning and startup. Its handler took 0.167 seconds.
- A second ordinary `spawn().get()` returned `Tesla T4, 15360 MiB` in 5.34
  seconds, task 654464de-6104-4e5e-9aa1-9f15e09e1928. This proves GPU access
  from a running workload, not a CUDA training benchmark. Published SDK 0.0.93.
- After both tasks exited, the platform automatically reduced the test GPU ASG
  to desired zero and the instance entered shutting-down. No ASG in us-east-1
  had positive desired capacity at the cleanup read. No manual EC2 launch or
  default purchase-policy change was needed.

Still to prove after release: GPU capacity-failure progress. Machine joining and
natural artifact retention require their recorded external prerequisites.
Do not convert these gaps into passes after deployment.

Final local cleanup deleted the four apps created for these follow-up checks:
sdk_user_functions, sdk_user_pod, sdk_user_services and user_checks_web. The
selected workspace's app list is empty. No unrelated workspace was deleted.

Fix verification cleanup: production workspace cli_reporting_fix_20260914 is absent
after deletion, although the DELETE request timed out. Its stalled build was stopped.
Local cli_reporting_local_20260914 was deleted because the existing local worker
belongs to mclean-connor and cannot serve the disposable workspace. Checks then used
that worker's workspace. The cancelled pending container was stopped explicitly;
the failure and timeout containers are no longer active. Both private test configs
select mclean-connor. No production release was changed.

External cleanup complete: S3 bucket
`lazycloud-sdk-check-5d95d2a0-20260914`, IAM user
`lazycloud-sdk-check-5d95d2a0` and its `sdk-bucket-only` policy/access key.
All were created by this pass after confirming their names were unused, then
deleted after the checks. AWS listings confirm the bucket and user are absent.
Deleted workspace secrets SDK_BUCKET_ACCESS and SDK_BUCKET_SECRET and removed
the revoked credential file.

## CLI

Commands below start with `lazycloud`.

- [x] CLI-01 Install the published CLI in a clean environment and open `--help`. PASS: `uv venv`, `uv pip install lazycloud-client==0.0.93`, `lazycloud --help`; installation succeeded and commands rendered, under 2 seconds.
- [x] CLI-02 Run `login` and use the authenticated account. PASS: `login --profile user-checks --endpoint https://lazycloud.dev --workspace mclean-connor` with the existing account token supplied privately through the environment; signed in and `workspace list` succeeded. Login 2 seconds, list 0.98 seconds. This verifies token login; fresh browser approval remains untested.
- [x] CLI-03 Cancel or deny a login and check the reported outcome. PASS: started device login for `cancelled-login`, received the sign-in prompt, pressed Ctrl-C; exit 130 in 1.31 seconds, no profile created. Existing profile retained.
- [x] CLI-04 Create, inspect, activate and delete a disposable `profile`. PASS: `profile set --profile disposable --no-activate`, `show`, `activate`, `current`, then restored `user-checks` and deleted `disposable`. Correct selection and inventory, 0.61–0.66 seconds per command.
- [x] CLI-05 Use `token set/show`; check invalid-token errors without exposing credentials. PASS: saved a deliberately invalid token in `disposable`; `token show` reported presence only, `workspace list` returned “invalid token” and exit 1 in 0.80 seconds. Disposable profile deleted.
- [x] CLI-06 Create, list, select and rename a disposable `workspace`. PASS: created `user_checks_20260914`, renamed it to `user_checks_20260914_run`, switched to `mclean-connor` and back; list shows the correct current workspace. Create 1.76 seconds, rename 0.91 seconds. Retained for subsequent checks and final deletion.
- [x] CLI-07 Run `quickstart` and execute the generated app. PASS: `quickstart --output quickstart.py`, `python quickstart.py`; printed greeting and `hello lazycloud`. Generate 0.59 seconds, execute 0.50 seconds.
- [x] CLI-08 Run `create-app` and execute the generated app. PASS: `create-app user_checks_starter`, `python user_checks_starter/quickstart.py`; returned `hello lazycloud`, 0.59/0.49 seconds. Initial hyphenated name was invalid and correctly rejected; follow-on file reads failed because that rejected scaffold did not exist.
- [x] CLI-09 Use `example list/download` and execute the downloaded example. PASS: `example list`, `example download quickstart --output downloaded`, `python downloaded/quickstart.py`; listed the example and returned `hello lazycloud`, 0.61/0.60/0.50 seconds.
- [x] CLI-10 Use `run` locally and check the return value. PASS: `run local_function.py:add 20 22` returned 42 in 0.63 seconds. Two-line plain Python function in the disposable directory's `workloads/`.
- [x] CLI-11 Use `run` remotely with typed arguments and check the return value. PASS: `run quickstart.py:hello Connor` returned `hello Connor` in 20.73 seconds, including a 15-second image build. `run functions.py:add 20 22` returned 42 in 6.53 seconds with the cached image. Both printed the handler's actual log message.
- [ ] CLI-12 Submit invalid arguments and check the error and nonzero exit status. PARTIAL: `run functions.py:add invalid 22` returned InvalidInputError and exit 1 in 9.87 seconds, but retried the invalid input four times and labeled it “Unexpected error.” Task `b85fa998-9a85-4be2-8a21-8d026f10ec5c`. Reject permanent input errors without retries and report them as input errors.
- [x] CLI-13 Run a handler that raises an exception and inspect its error and exit status. PASS: `run functions.py:fail` reported the exact deliberate ValueError and exit 1 in 10.90 seconds. Four attempts and their tracebacks appeared under the default retry policy.
- [ ] CLI-14 Inspect a submitted run with `task list/show/result`. PARTIAL: list/show correctly inspected `651c0f19-a176-42b8-9a8c-1d999eebbd05`, but `task result` returned a cloudpickle descriptor instead of 42 in 1.04 seconds. Provide a useful result without unsafe deserialization. The public CLI has no detached-run flag.
- [x] CLI-15 Read historical and live output with `logs` and `task logs`. PASS: `task logs 651c0f19-a176-42b8-9a8c-1d999eebbd05` returned the add message in 0.85 seconds. `logs --task-id a318db06-c140-4c91-85c7-273e67087ed4 --follow --lines 1 --max-events 4` showed tick 11, then newly emitted ticks 12–14 and exited.
- [ ] CLI-16 Cancel pending work with `task cancel` and verify it never executes. PARTIAL: `run functions.py:add 20 22 --pool user_checks_empty` stayed pending. Cancelled `af82199c-2f16-4127-bf5a-a266953af6b8`; it never started, but the run called cancellation “Unexpected error.” A pending container remained and required explicit `container stop 9bc95751-64dc-4fa3-934d-99bfe815317a`. Check cancellation cleanup.
- [ ] CLI-17 Stop running work with `task stop` and verify it stops. PARTIAL: stopped running task `a318db06-c140-4c91-85c7-273e67087ed4`; `task show` reported cancelled and output stopped after tick 21. Original command exited after 24.41 seconds, but called the requested cancellation “Unexpected error” and suggested debugging. Report cancellation clearly.
- [x] CLI-18 `deploy` a function and invoke the deployed version. PASS: deployed cli_deploy.py:app with the published CLI. Ordinary authenticated HTTP POST to its public URL returned task b00c2e9c-2437-424b-aa7f-dbb309c3baab in 0.96 seconds; task result printed version=1, value=42. No generated client used.
- [x] CLI-19 Deploy a changed function and verify new calls use the change. PASS: changed the function to return version=2 and redeployed through the CLI. POST to the same public URL returned task f97262c3-8c14-4d93-8da8-69beee8826b9; task result printed version=2, value=42.
- [x] CLI-20 Deploy an endpoint and call its URL with an ordinary HTTP client. PASS: `deploy web.py:app` took 1.78 seconds. `curl -H 'Content-Type: application/json' -d '{"name":"Connor"}' https://hello-d7342fd7.lazycloud.dev` returned HTTP 200 and `{"message":"hello Connor"}` in 6.19 seconds, including startup. No retry.
- [x] CLI-21 Inspect the deployed app with `app list/show` and `deployment list`. PASS: `app list`, `app show user_checks_web`, `deployment list --app user_checks_web` showed the correct app and active endpoint v1, 0.86–1.09 seconds each.
- [ ] CLI-22 Use `deployment stop/start` and check availability after each action. PARTIAL: stop blocked requests but returned HTTP 405 “Method Not Allowed” for the same valid POST. Start restored HTTP 200 with `hello restarted` in 3.94 seconds. Report unavailable deployment clearly.
- [ ] CLI-23 Use `deployment scale` on a Pod and inspect the actual replica count. PARTIAL: `deployment scale user_web_pod --containers 2` produced two running servers. Scaling back to one returned success in 1.07 seconds, but repeated inventories still showed both running minutes later. Diagnose scale-down before calling it working. The CLI documentation also incorrectly shows endpoint scaling; that attempt returned “only pod deployments can be scaled directly.”
- [ ] CLI-24 Use `app pause/resume` and check workload behavior. PARTIAL: `app pause user_checks_web` blocked requests but returned the misleading HTTP 405. `app resume user_checks_web` restored HTTP 200 with `hello resumed` in 7.48 seconds. Same error-reporting issue as CLI-22.
- [ ] CLI-25 Run `serve` and call the selected resource's preview URL. PARTIAL: `serve web.py:app --resource endpoint:hello` printed a URL and “Serving,” but the first POST returned HTTP 503 “endpoint container is not ready” in 3.05 seconds despite running container status. A later request after source reload returned HTTP 200 in 1.54 seconds. First failure retained.
- [x] CLI-26 Edit a file during `serve` and observe the changed response and live logs. PASS: changed the response to `updated hello`; CLI printed “Synced 1 changed” and “endpoint handler refreshed.” POST to the same preview URL returned `{"message":"updated hello preview"}` in 1.54 seconds. Initial preview failure remains in CLI-25.
- [x] CLI-27 End `serve` manually and verify its preview resources stop. PASS: Ctrl-C printed “Stopping serve container,” returned the prompt, and container `1aa45c2d-fdb3-4fe7-a8c6-25ffcce8883e` became stopped. Preview session lasted 91.80 seconds including manual checks.
- [x] CLI-28 Use a `serve` timeout and verify automatic exit and cleanup. PASS: `serve web.py:app --resource endpoint:hello --timeout 8` exited 0 without intervention in 13.83 seconds including setup. Container `f87605b6-e5b4-4715-81c1-db21c73c00cb` became stopped with timeout exit 559. No unrelated request or sync assertions were imposed on its short lifetime.
- [x] CLI-29 Open `shell` on a running workload and execute an interactive command. PASS: `shell --container-id 748468a7-ed48-4b33-ad55-05c4fd2f42e4` opened a prompt; typed printf and received `terminal-user-check` from the container.
- [x] CLI-30 Check terminal input, resize, Ctrl-C and exit status in that shell. PASS: Ctrl-C interrupted `sleep 60` and retained the shell. Resizing the local PTY from 30×120 to 40×100 changed remote `stty size`. Remote `exit 7` propagated exit 7 to the local shell. Session 28.67 seconds.
- [x] CLI-31 Use shell file sync and read the edited file inside the container. PASS: `shell --container-id 748468a7-ed48-4b33-ad55-05c4fd2f42e4 --sync-dir .`; created `sync-note.txt` locally during the session and read its exact content under `/mnt/code`. An immediate read preceded sync and returned “No such file”; the next read after inspecting the sync path succeeded. No missing-file result concealed.
- [ ] CLI-32 Run `dev`, execute a command and exit cleanly. PARTIAL: `dev pod.py:web --sync .` eventually executed typed printf commands, but output arrived in a delayed burst after repeated checks. Session lasted 51.48 seconds. Prompt started in `/workspace`, where the synced `sync-note.txt` was absent. Check terminal latency and sync location; ordinary shell attachment in CLI-29 was responsive.
- [ ] CLI-33 Use `container list/attach` and observe real output from a running container. PARTIAL: attachment showed historical and new request logs correctly. After deployment stop, it reported “Exit code 0,” while container inventory recorded stop exit 560 for `748468a7-ed48-4b33-ad55-05c4fd2f42e4`. Check terminal-result reporting. The `/attach-check` file-missing 404 was deliberate.
- [x] CLI-34 Use `container checkpoint` and inspect the saved checkpoint. PASS: after confirming the restored Pod was running, `container checkpoint 15b9ac8c-f97f-4c0d-8635-66f5d1ebed1c` returned checkpoint `e99cc463-261d-43fe-b827-62cd76b9aec5` in 9.76 seconds. Its durable record is available, 147,302,400 bytes, scoped to the test workspace and source container. Initial automatic snapshot startup took minutes; public requests timed out at 30 and 15 seconds while pending. Preserve that startup concern for SDK-12/46. Deployment stopped after the check.
- [x] CLI-35 Use `container stop` and verify the selected container stops. PASS: stopped the test dev container `97d3eda1-61dc-4352-a185-34d8a4a26843` in 1.00 second; final inventory shows exited with code 0. Both test Pod replicas also became stopped after their deployment was stopped.
- [x] CLI-36 Create a `volume`, upload with `cp`, list with `ls` and download identical bytes. PASS: `volume create user_check_files`, uploaded `sync-note.txt` to `lazycloud://user_check_files/note.txt`, listed 45 bytes, downloaded to `downloaded-note.txt`; `cmp` returned 0. Commands 0.94–1.21 seconds.
- [x] CLI-37 Rename with `mv`, remove with `rm` and delete the test volume. PASS: renamed note.txt to renamed.txt, listing confirmed it, removed the file, confirmed an empty listing, deleted `user_check_files` and confirmed no volumes remain. Commands 0.92–1.18 seconds.
- [x] CLI-38 Create, inspect and modify a disposable `secret`; verify a new workload sees the change. PASS: created `USER_CHECK_VALUE` with a non-sensitive marker, `secret show` masked it, and `run secrets_demo.py:matches` returned Yes for v1 and then v2 after `secret modify`. The consumer uses keep_warm=0; calls took 6.62/5.49 seconds. Values are compared inside the workload, not returned.
- [ ] CLI-39 Delete the test secret and check the error from a new consumer. PARTIAL: `secret delete USER_CHECK_VALUE` succeeded; the next run failed with exit 1 in 7.09 seconds and named the missing secret. Output wrapped it as “Unexpected error” with internal hydrate-credentials/WorkerRepositoryClientError details. Present a direct missing-secret error.
- [x] CLI-40 Use `artifact list/usage/delete` on a real workload artifact. PASS: `run artifacts.py:make_report` created `c0519b6b-3fb0-48fa-83e1-004fa2d4bdfc`; list/usage showed user-report.txt, 35 bytes and one artifact. Confirmed its deletion and empty final listing. Read commands about 1 second. Test-operation mistake: queued shell commands were rejected as invalid answers at the delete confirmation; answering y completed deletion in 16.72 seconds including that delay.
- [x] CLI-42 Inspect `compute status/instances/pools/workloads` and match a workload to its placement. PASS: commands showed the lazycloud pool and active function/endpoint placements. No customer cloud is connected, so its instance list is empty. A completed explicit-pool call ran in `8dd25ec7-97a7-46d7-9a5c-db90cd64a1b3` on platform machine `bf71a050-8077-57fa-b36e-bdc516283604`; operator inventory confirms it belongs to the platform unit. Read commands 0.88–1.06 seconds.
- [x] CLI-43 Use `compute policy show/update` and verify default versus explicit pool selection. PASS: changed default to `user_checks_empty`; unpinned call stayed pending while `run functions.py:add 20 22 --pool lazycloud` returned 43. Cancelled pending work and restored default `lazycloud`. Its leftover pending container required an explicit stop, the same cleanup concern tracked in CLI-16. Only one pool has capacity, so this proves selection and override, not successful placement in two healthy pools.
- [ ] CLI-44 Use `machine list` and match its entries to the selected workspace. PARTIAL: the empty inventory is correct for this fresh workspace and returned in 0.90 seconds. Joined-machine visibility awaits CLI-46.
- [ ] CLI-45 Use `machine join --print-only` on a disposable host without changing that host. PARTIAL: `machine join --pool user_checks_machine --ttl 2m --print-only` exited 0 in 0.98 seconds and printed an install command to a private file. No agent was started. Executing the command on a suitable host remains unverified.
- [ ] CLI-46 Join a disposable machine in foreground mode and run work on it. BLOCKED: current host UID 1000; `sudo -n -l` requires a password. No privileged disposable host is prepared. No install attempted.
- [ ] CLI-47 Join a disposable machine in service mode and run work after a service restart. BLOCKED: same host-privilege prerequisite as CLI-46.
- [ ] CLI-48 Remove the test machine and verify host cleanup and stopped scheduling. BLOCKED: no machine joined in this pass. Do not remove platform or pre-existing machines.
- [x] CLI-49 Use `--json` on successful and failed commands; check parseable output and exit status. PASS: `--json workspace list` produced parseable JSON in 0.87 seconds. Selecting a nonexistent workspace returned exit 1, empty stdout and a JSON not_found error on stderr in 0.96 seconds.
- [x] CLI-50 Run `update` in a disposable installation and retain working authentication. PASS: installed published 0.0.92 in `update-venv`, ran its `lazycloud update`, verified package version 0.0.93 and successful authenticated `workspace list`. Exit 0; the primary test environment was untouched.
- [x] CLI-51 Check a failed update leaves the previous installation usable. PASS: in a separate 0.0.92 installation, ran `update` with the package index deliberately set to an unreachable local port. It reported connection refused, exited 2 and did not claim success. Installed version remained 0.0.92; authenticated `workspace list` still worked.
- [x] CLI-52 Delete test deployments, apps and workspace; verify no test workload remains active. PASS: deleted user_add and the four named test apps, confirmed no apps/deployments or pending/running containers, then deleted `user_checks_20260914_run` in 4.69 seconds. Final workspace list preserves default, mclean-connor and the older release-check workspace; the current run's workspace is absent.

## SDK

Use public imports and small examples, one behavior at a time.

- [x] SDK-01 Install and import the published SDK on each supported Python version. PASS: fresh installs imported 0.0.93 on Python 3.10.20, 3.11.15, 3.12.11, 3.13.6 and 3.14.6. Each import took about 0.5 seconds. Venv creation from the repo warned about backend packages requiring 3.12; the independent published SDK installations succeeded.
- [x] SDK-03 Call a function with missing or invalid credentials and an inaccessible workspace; check clear errors. PASS: add.remote with invalid and missing tokens raised FunctionOperationError naming the problem in under one second. In SDK-74, a running function's scoped credential rejected a volume read in the other test workspace with token is not authorized for this workspace. Earlier Client-facade checks are excluded.
- [x] SDK-04 Run a function locally and check its value. PASS: imported add from `workloads/sdk_functions.py`; `add.local(20, 22)` printed its log message and returned 42 immediately.
- [x] SDK-05 Call a function with `.remote()` and check its value. PASS: `with output(): print(add.remote(20, 22))` returned 42 in 21.38 seconds in the fresh SDK workspace. Actual output showed the 15-second image build, task pending/running/completed states and handler log.
- [x] SDK-06 Use `.spawn()` and retrieve the completed result from its handle. PASS: `call = add.spawn(10, 7)` returned task `9d650545-2295-48f4-bd29-92ccee97a31a`; `call.get()` returned 17.
- [x] SDK-07 Use the async function APIs from an ordinary async program. PASS: `asyncio.run(add.async_remote(6, 7))` returned 13 in 5.79 seconds with observed task lifecycle and logs.
- [x] SDK-09 Send typed inputs and check the returned structured value. PASS: `summarize.remote([2, 3, 5], enabled=False)` returned `{"total": 10, "enabled": False}` in 4.48 seconds, preserving list inputs and the explicit false value.
- [x] SDK-10 Send invalid input and verify rejection before handler execution. PASS: `add.remote("invalid", 22)` raised FunctionOperationError carrying InvalidInputError in 3.35 seconds. Runtime traceback shows argument coercion rejected it before the handler body; its “adding” message was absent. This example explicitly sets retries=0; default retry behavior remains open in CLI-12.
- [x] SDK-11 Raise an exception remotely and inspect the client-visible failure. PASS: `fail.remote()` raised FunctionOperationError with the exact deliberate ValueError in 4.56 seconds. Remote traceback identified the line in sdk_functions.py. One attempt with retries=0.
- [x] SDK-12 Observe a cold start and check progress, completion and elapsed time. PASS: first `square.remote(4)` prepared a new function runtime, showed pending/running/complete and returned 16 in 3.84 seconds with an already built image. This verifies ordinary function startup; the slower checkpoint-enabled Pod startup remains for SDK-46.
- [x] SDK-13 Use `.map()` on a small input list and check every ordered result. PASS: `list(square.map([0, 1, 2, 3]))` returned `[0, 1, 4, 9]` in 2.47 seconds.
- [x] SDK-14 Gather concurrent calls and check results and completion ordering. PASS: ordered gather returned `[25, 36, 49]` for three square calls. `TaskBatch(...).as_completed()` returned both slow-call task IDs in their completion order. The shared function runtime serialized these calls; this does not prove parallel handler execution.
- [x] SDK-15 Read task status, timing, events and logs for a known task. PASS: status complete, creation/start/finish timestamps and attempt counts match the run. `call.logs()` and `call.output()` returned `adding 10 and 7`. Subscription returned complete for the finished task and running for a live slow task. The API returns a current status snapshot, not a historical transition stream; no full-history claim.
- [ ] SDK-16 Cancel pending work and verify it never executes. PARTIAL: explicit empty-pool task `6438a83c-0abf-4533-8246-3d121bd5b6d6` showed capacity_unavailable and no handler output. `.cancel()` returned it in stopped, but pending container `0f2d8f5b-e436-49f4-ac6f-4186bf02d163` remained and needed explicit stop. Same cleanup issue as CLI-16.
- [x] SDK-17 Cancel running work and verify it stops. PASS: observed running task `56a28e15-146c-454d-976c-8234218465ea` emitting ticks, then `.cancel()` changed it to cancelled. Output stopped at tick 14 across subsequent reads. `.get()` raised TaskOperationError request_cancelled.
- [x] SDK-18 Rerun a task and check the result stays in the selected workspace. PASS: `call.rerun()` created `ea4249d6-806e-4d13-99b5-88d9d8e47d6c`; `.get()` returned 17. Its durable workspace matches the original, `5d95d2a0-f031-462c-b1b3-b559b731b012`.
- [x] SDK-19 Set a task timeout and verify the final outcome and cleanup. PASS: task `be58c063-e028-49d3-833b-eef02eb45f39` timed out after 3.32 execution seconds with timeout_seconds=3, exit 124 and TaskOperationError function execution timed out. Its container stopped automatically. Test setup initially used unsupported timeout=3; corrected to the documented timeout_seconds before invocation.
- [x] SDK-20 Configure retries and observe each attempt and the final result. PASS: retries=1 produced exactly two logged handler attempts on task `9b871b33-0db3-4bb4-a8d7-78fd651d4f5c`. Final state failed, attempt_number=max_attempts=2, exact deliberate ValueError preserved.
- [x] SDK-21 Receive a completion callback at a real receiver. PASS: deployed a real ASGI receiver and verified HTTP 200 before submission. callback_work returned its value in 5.65 seconds. Receiver logs show task `115a6d97-3995-4e86-8b63-f06324678451`, status complete and HTTP 200 for the actual callback POST.
- [x] SDK-22 Schedule a function, observe execution and remove the schedule. PASS: deployed sdk_scheduled with cron="* * * * *". Scheduler created task `03ad440e-71b7-4837-bdcf-1f03cef6b517` at 18:21:04 UTC; it completed and logged scheduled execution. Deleted its exact deployment through the public CLI after the run.
- [ ] SDK-23 Set a warm replica count and verify reuse and cleanup after deletion. PARTIAL: min=max=1 reused container `69b81357-f28c-47c3-a793-52ce5939937c` and PID 1 across two calls, 4.2/0.8 seconds. Deployment deleted. Preparation also launched container `804b577c-b041-4daa-b6e1-9ebedf7eef0d`, which stopped with startup error "a terminal container cannot restart" during mark-running. Diagnose this deployment/startup race before calling the whole workflow clean.
- [x] SDK-24 Deploy an app with multiple resources and invoke each selected resource. PASS: `services.deploy()` created four selected resources. Invoked greet and protected with correct values, the ASGI receiver returned accepted, and realtime echo returned echo: hello. Each resource ran through its own public URL or resource object.
- [x] SDK-25 Update a deployment and check current and pinned version behavior. PASS: greet v2 returned hello v2 current; the same resource pinned with deployment_version=1 returned hello pinned. No source swap needed to call the retained version.
- [ ] SDK-26 Pause, resume, scale and delete a Pod through its SDK object. PARTIAL: deployed sdk_web_pod, scaled to two servers then one, observed one running replica after scale-down, paused and confirmed stopped, resumed and got HTTP 200, then deleted. This Pod used keep_warm=5 and repeatedly expired/replaced idle replicas. A final replacement `c0ea8c59-66bc-4316-a2e6-c230274a4140` ended failed before assignment during deletion, without logs. Control actions worked; check minimum-replica TTL and deletion-race reporting before calling the full workflow clean.
- [x] SDK-27 Call an endpoint with an ordinary HTTP client and inspect status, headers and body. PASS: httpx POST to greet returned HTTP 200, application/json and `{"message": "hello SDK user"}` in 9.62 seconds including first startup.
- [x] SDK-28 Check endpoint authentication rejects unauthorized calls before handler execution. PASS: authenticated `protected.target("deployed").request(41)` returned 42 with HTTP 200. Missing and invalid tokens each returned HTTP 401 with the correct reason. Task inventory and handler logs contain only the authenticated invocation.
- [x] SDK-29 Call an ASGI app and verify its HTTP response. PASS: `callback_receiver.request(method="POST", ..., target="deployed")` returned HTTP 200 and accepted. Receiver logs confirm the submitted asgi-user-check marker.
- [x] SDK-30 Exchange messages over a real WebSocket connection. PASS: websockets.sync.client.connect to the deployed echo URL, sent hello and received echo: hello, then closed the connection.
- [ ] SDK-31 Send concurrent endpoint requests and inspect every response and elapsed time. PARTIAL: five ordinary httpx POSTs hit the 30-second read timeout although all five task records later showed complete and handler logs contained all five names. Example task spent 26.8 seconds between creation and handler start, then executed in 0.33 seconds. First asyncio.gather raised before printing individual results. Repeat with return_exceptions=True and a 60-second timeout returned five ReadTimeout exceptions. Durable dispatch records show those five eventually completed after about 87 seconds pending, on a replacement container `5198b386-ea9a-4f0e-b8bb-72eeac562cfa`, each handler under 0.3 seconds. Diagnose delayed routing/replacement; no claim that more compute is the cause.
- [ ] SDK-32 Request a starting or stopped endpoint and inspect the actual failure or recovery. PARTIAL: valid POST after stopping greet v2 hit the 15-second read timeout instead of a clear unavailable response. Starting v2 restored HTTP 200 with hello v2 restarted. v1 remains retained from the pinning check, so diagnose version/routing behavior as well as the latency in SDK-31.
- [ ] SDK-33 Use `.serve()` for a selected resource and call its preview URL. PARTIAL: `app.serve(resource="endpoint:greet", timeout=90)` printed Serving and its URL. First ordinary POST returned HTTP 503 endpoint container is not ready in 3.00 seconds, matching CLI-25.
- [x] SDK-34 Edit preview source and verify the changed response; close the preview cleanly. PASS: edited sdk_services.py during serve, observed Synced 1 changed and endpoint handler refreshed, then HTTP 200 with preview edit SDK preview in 1.05 seconds. Serve exited 0 and printed Stopping serve container. Initial 503 remains in SDK-33.
- [x] SDK-35 Build an image with a Python dependency and import it in a workload. PASS: built Image(python_packages=["six==1.17.0"]), imported six remotely and returned version 1.17.0. Total 17.59 seconds including a 13-second image build.
- [x] SDK-36 Build an image with a system package, copied file and build command; use their outputs. PASS: Dockerfile COPY plus Image.add_commands installed jq and wrote a marker. Remote workload returned jq-1.7, the copied file's exact text and build command worked. Total 21.95 seconds including a 17-second build.
- [x] SDK-37 Build from a Dockerfile and run the resulting image. PASS: `Image.from_dockerfile("Dockerfile.user-check", context_dir=".")` built successfully; the SDK-36 workload executed in that image and read its copied file.
- [x] SDK-38 Use a supported project dependency file to build and run its workload. PASS: created a small pyproject.toml and uv.lock, used Image.add_uv_project("image_project"), then imported six remotely and returned 1.17.0. Total 20.52 seconds with a 15-second frozen dependency build.
- [ ] SDK-39 Break an image build deliberately and inspect its error and logs. PARTIAL: deliberate exit 17 correctly prevented workload execution and raised FunctionOperationError after 24.32 seconds. The builder unnecessarily reran the same failing command with vfs after overlay failed, then repeated the entire build log in several error layers. Build `4e9cd279-503d-47f2-a3bc-1e5f22137bad`. Stop retrying deterministic Dockerfile failures and report the failed step directly.
- [ ] SDK-40 Run with explicit CPU, memory, environment and pool settings and inspect them inside the workload. PARTIAL: cpu=0.5, memory=256Mi, pool=lazycloud submitted successfully and returned the configured environment marker. Initial probe incorrectly assumed cgroup v2 files were mounted and failed with FileNotFoundError. Corrected diagnostic shows virtual cgroup v1 membership with no exposed cgroup filesystem; quota enforcement needs host evidence, not that invalid in-container file probe. Latest task `83345884-f386-4d73-8d9d-6efcd9416738`.
- [ ] SDK-41 Run a GPU workload on matching hardware; check invalid or unavailable GPU requests. PARTIAL: On-Demand T4 provisioning and two production tasks passed, including an SDK result of `Tesla T4, 15360 MiB`. Spot requests hit AWS capacity shortages across multiple zones. Published SDK 0.0.93 still hides some acquisition failures behind Starting compute; verify the PR's reporting fix after release. Cancelled unsuccessful tasks, stopped their pending containers, and observed automatic GPU worker scale-down. Invalid GPU names are rejected before submission. The first pass briefly reused an already-prepared definition, so its zone override did not apply; subsequent placement checks used fresh definitions.
- [x] SDK-42 Start a Pod, connect to the running instance and terminate it. PASS: web.create created `18a0e4f6-c73a-4116-8918-a7aa534be347`, inventory confirmed running, its HTTP server responded and SDK shell sessions connected twice. PodInstance.terminate returned True.
- [x] SDK-43 Run a server in a Pod and call its exposed HTTP port. PASS: ordinary httpx GET to the on-demand Pod URL returned HTTP 200 with the expected directory listing.
- [ ] SDK-44 Exchange data through an exposed TCP port where ingress is configured. PARTIAL: tcp=True created a healthy TCP echo server, verified by a real socket inside its Pod. SDK returned an HTTPS URL; sending raw bytes over TLS to that advertised host produced an HTTP 400 from the HTTP ingress. No working raw-TCP address was supplied. Fail clearly when TCP ingress is unavailable, or provide the correct TCP connection details. Pod terminated.
- [x] SDK-45 Check Pod health handling using a server that becomes unhealthy. PASS: healthy server returned 200. After its /fail action, an immediate request still passed under cached health; the next recorded /health probe returned 503 about 16 seconds later, and public requests then returned 503 pod container is not ready. Terminated the test Pod afterward.
- [ ] SDK-46 Restore a Pod checkpoint and verify retained process state. PARTIAL: automatic snapshot cc0048e4-eb50-4b79-9beb-378dbc0d14aa became available, 1,218,652,160 bytes. Original server returned in-memory count 1003 and PID 1, then terminated. A second create returned container 11288b74-3f2b-440d-85aa-09270fb2e4b7 in 0.54 seconds, but its URL returned 503 pod container is unavailable. Snapshot status became restore-failed; container logs report cannot read client sync file, waiting for sandbox to start: EOF. No restored state was observed.
- [x] SDK-47 Create a sandbox, reconnect by ID, inspect it and terminate it. PASS: created `4277e0d7-6fd4-45c3-9043-ee1fddb010f5`, reconnected through sandbox.connect and ran commands through the reconnected object. Terminated through its async API after file/network/snapshot checks.
- [x] SDK-48 Execute a sandbox process and inspect stdout, stderr and exit status. PASS: a real Python process printed separate stdout/stderr markers and exited 7. instance.run returned both exact streams and exit_code=7.
- [x] SDK-49 List and kill a running sandbox process and verify it stops. PASS: started sleep 60 as PID 14, observed it in instance.list_processes, killed it and read status `(143, "exited")`.
- [x] SDK-50 Use the async sandbox APIs and verify process completion and cleanup. PASS: await instance.aio.run returned exit 0 and 42 from a real Python process; await instance.aio.terminate completed cleanup afterward.
- [x] SDK-51 Upload, list, inspect, download and delete sandbox files with matching bytes. PASS: uploaded a 16-byte file, list/stat agreed, downloaded identical bytes, then deleted and confirmed an empty directory.
- [x] SDK-52 Find and replace text in sandbox files and read the changed content. PASS: found alpha at line 1, columns 1–6; replaced it with beta and read beta user file through a real process. File removed afterward.
- [x] SDK-53 Start a sandbox HTTP server, confirm it listens, expose its port and call the URL. PASS: started Python http.server on port 8080, verified localhost HTTP 200 inside the sandbox, called instance.expose_port(8080) and received public HTTP 200.
- [x] SDK-54 Change sandbox network permissions and verify allowed and blocked traffic. PASS: block_network=True made a TCP connection to 1.1.1.1:443 time out. Allow-listing 1.1.1.1/32 allowed the same connection. Restored unrestricted policy and verified the returned policy has no allow-list.
- [x] SDK-55 Update a sandbox TTL and observe actual expiry. PASS: created `fda5ab39-2bb9-425c-8ce6-64e7ce95cd95`, updated TTL to 8 seconds and observed its automatic stopped state without manual termination.
- [x] SDK-56 Create a filesystem image and start a new sandbox with its saved files. PASS: wrote persisted-sandbox-data, created image img_56629f5bb303d7d7a9920228 in 14.97 seconds, started a fresh sandbox with Image.from_id, and read the exact saved text. Terminated restored sandbox `61ab2e2c-44d8-43a5-9de8-772531871e32` afterward.
- [x] SDK-57 Restore a sandbox memory snapshot and verify retained process state. PASS: a live HTTP process at PID 85 returned in-memory counter 1001 before the snapshot. Snapshot took 25.91 seconds, restore 4.77 seconds; restored process retained PID 85 and returned counter 1002 without relaunching the server. Terminated restored sandbox `84f06218-c865-4681-92b6-7e5c53d4942a`; inventory confirms stopped, though its stop exit code is 1 and merits review with other lifecycle reporting.
- [ ] SDK-58 Build and run a Docker container inside a sandbox; inspect logs and stop it. PARTIAL: docker_enabled=True with the default Python image failed because dockerd was absent; sandbox docs need Image.with_docker() too. Corrected build `6a26d91d-77ff-494c-9581-dae69e46e4ef` then failed publishing image content at 18:43:13 UTC, "content cache unavailable." Docker execution remains untested behind this real build/cache failure. Failed sandbox `07a8678c-59ec-46ff-9a9a-327d2fcf140b` is terminal.
- [ ] SDK-59 Push and pull a test image through an authorized registry and remove test objects. BLOCKED by the Docker image/cache failure in SDK-58. No registry objects created.
- [ ] SDK-60 Run a small Docker Compose app inside a sandbox and remove its containers and volumes. BLOCKED by SDK-58. A small Compose file is prepared under the disposable workload directory; no nested containers or volumes created.
- [x] SDK-61 Open a terminal on a running workload, exchange input/output and reconnect. PASS: Shell.create_existing plus Shell.connect opened an actual PTY on the running Pod. Typed sdk-terminal-marker, exited 0, reconnected and typed sdk-reconnected. Remote exit 3 propagated as process exit 3.
- [x] SDK-62 Mount a `Volume`, write a file and read it from a later workload. PASS: write_file mounted sdk_user_volume at /data and wrote durable user data; separate read_file workload returned that exact text. keep_warm=0 avoids relying on the original container.
- [x] SDK-63 Upload and download files through Volume methods and compare bytes. PASS: volume.write_bytes followed by read_bytes preserved `b'\x00\x01user\xff'` exactly.
- [x] SDK-64 Mount an existing `CloudBucket` with workspace secrets and verify reads and writes natively. PASS: created a dedicated S3 bucket and IAM user restricted to that bucket, stored its credentials as workspace secrets, mounted the bucket and wrote/read native bucket data. AWS's object listing confirms user-prefix/note.txt, 18 bytes. Workload took 8.2 seconds.
- [ ] SDK-65 Select a bucket prefix and check read-only and invalid-credential behavior. PARTIAL: prefix mount listed only note.txt despite an outside-prefix object in S3. Read-only writes raised OSError 30. Invalid secret correctly rejected mount startup with S3 SignatureDoesNotMatch/403 before the handler ran, but the SDK repeated a long internal mount traceback instead of a direct credential error. Task `95e078e4-0414-4be5-b6bb-25bf248be644`.
- [x] SDK-66 Save and retrieve an `Artifact` and compare its bytes. PASS: remote Artifact.file(...).save created a real artifact; HTTP retrieval through its signed URL returned 200 and exact artifact user bytes.
- [x] SDK-67 Check signed artifact URL access, expiry and deletion behavior. PASS: five-second URL initially returned HTTP 200 and then 403 Request has expired. Deleted a separate artifact while its five-minute URL was valid; that URL returned 404. Final artifact listing is empty.
- [ ] SDK-68 Observe natural artifact retention expiry before claiming retention works. BLOCKED by the actual retention window. This account's newly uploaded artifact expires on 2026-12-13. No clock/row edits or shortened policy used; metadata alone does not prove natural expiry. Signed-URL expiry is separate in SDK-67.
- [x] SDK-69 Inject a `Secret` into a runtime and build; verify updated values in new consumers. PASS: random value matched its digest inside both runtime and image build. Updating the secret and preparing a fresh definition rebuilt the image; both checks returned True for the new value. Secret deleted. Test interference: creating the isolation workspace changed the active profile during one build, which failed with source artifact unavailable; repeated with a stable workspace and it passed. Never change the active profile during a request.
- [x] SDK-70 Use a `Map` across separate workloads and verify shared values and deletion. PASS: publisher stored value=42, a separate consumer read 42, local Map read matched, then deletion removed the key and collection.
- [x] SDK-71 Use a `Queue` across separate workloads and verify message delivery and removal. PASS: publisher put a JSON job, separate consumer popped the exact object, queue.empty returned True, then the test queue was deleted.
- [x] SDK-72 Set and observe an experimental signal between workloads and clear it. PASS: one function set sdk_user_signal, another read True, then signal.clear followed by monitor_once returned False.
- [ ] SDK-74 Check workspace isolation using named test resources and inaccessible resource IDs. PARTIAL: scoped runtime credentials correctly rejected a named volume in sdk_isolation_20260914. That workspace's real sandbox was running and its owner could execute commands. A function in the main workspace tried connecting by its ID and got a 60-second internal proxy transport timeout instead of a prompt authorization/not-found error. Task `f6840196-26d3-4850-93ab-0f356ecfefb5`; no unauthorized access observed, but the failure path is not acceptable. Stopped the foreign test sandbox afterward.
- [x] SDK-75 Delete test resources, restore changed settings and verify no active test workloads remain. PASS: removed this pass's apps, deployments, volume files and volumes, artifacts, secrets and collections. Confirmed no pending/running containers in either workspace, then deleted user_sdk_checks_20260914 and sdk_isolation_20260914. Public App has no delete method; app cleanup used the CLI. Platform inventory showed two workers at its configured floor, one available and one cordoned for replacement. That infrastructure degradation is separate from workspace cleanup.
