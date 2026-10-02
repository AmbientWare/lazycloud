# Parity checklist

Every user-visible capability of the reference at 9e259ce75. The rewrite
matches each one (update.md, "Same product, better implementation"). Check an
item when the rewrite provides it and its area's acceptance covers it; record
intentional differences in the area's task file.

Path prefixes, all absolute:
- `R` = /home/cmclean/.t3/worktrees/lazycloud/t3code-9782b237
- `SDK` = R/packages/lazycloud/src/lazycloud
- `CLI` = SDK/cli
- `WEB` = R/apps/web/src
- `Q` = WEB/lib/queries
- `DOCS` = R/docs
- `SH` = R/packages/shared/src/shared

Scope notes:
- The public `lazycloud` CLI is in `SDK/cli` (entry `lazycloud.cli.main:start`). R/apps/cli is `lazycloud-admin`, which is operator-only and left out here.
- Global flags `--json`, `--debug` and `-h/--help` work in any position before `--` (CLI/main.py:152). `--workspace` on a command overrides `LAZYCLOUD_WORKSPACE`, which overrides the stored profile.
- Some areas have no CLI at all: members and invitations, token creation, billing, queues and maps, and metrics. They exist only in the dashboard or SDK.

## Auth and accounts
- [x] Dashboard sign-in, GitHub only; first sign-in creates the account and an owned workspace; browser session lasts 12h (DOCS/platform/auth.mdx)
  Delivered: internal/identity signin, internal/api/browser.go; identity sign-in tests (`TestSignInFailuresRedirect` and the first-sign-in tests). Real GitHub unverified locally (tasks/identity.md Gaps).
- [x] `/signin?error=` page with a closed set of error messages (access_denied, invalid_state, account_disabled, …) (WEB/routes/signin.tsx)
  Delivered: web/src/routes/signin.tsx with the same closed set plus `invalid_return_to`/`provider_refused`; `TestSignInFailuresRedirect`.
- [x] `/callback` redeems the GitHub code from the URL fragment, clears it from history, redirects to return_to or /dashboard (WEB/routes/callback.tsx)
  Intentional: web/src/routes/callback.tsx. The server sets an HttpOnly cookie and the fragment carries only the destination: tasks/identity.md, tasks/web.md.
- [x] Sign out ends only the current browser session (Q/auth.ts `signOut`, WEB/components/shared/AppShell/AccountRail.tsx)
  Delivered: Q/auth.ts `signOut` over the session delete; identity session tests.
- [x] `lazycloud login` device-code flow: card shows profile, endpoint, verification link and code; polls until approved, denied or expired (CLI/identity.py:107,191)
  Delivered: cli/identity.py, same card; device-flow tests in python/lazycloud/tests (also honours `slow_down`).
- [x] `login` flags: `--profile`, `--endpoint`, `--workspace`, `--token`, `--tls/--no-tls`, `--activate/--no-activate` (CLI/identity.py:191)
  Intentional: same flags (help-tree diff empty). Stored workspace and the several-workspace refusal: tasks/identity.md.
- [x] `/activate` page: enter code XXXX-XXXX, Approve or Deny CLI sign-in, "CLI connected" / "Sign-in denied" states (WEB/routes/activate.tsx, Q/auth.ts approve/deny)
  Delivered: web/src/routes/activate.tsx, Q/auth.ts approve/deny; identity device tests.
- [x] `lazycloud profile list|current|show [--profile]|set [--profile --endpoint --workspace --token --tls/--no-tls --activate/--no-activate]|activate NAME|delete NAME` (CLI/identity.py:273-400)
  Delivered: cli/identity.py; help-tree diff empty; profile CLI tests.
- [x] `lazycloud token set VALUE [--profile]`, `token show [--profile]` (stored copy only) (CLI/identity.py:402,425)
  Delivered: cli/identity.py; code reading only.
- [x] Environment variables `LAZYCLOUD_TOKEN`, `LAZYCLOUD_WORKSPACE`, `LAZYCLOUD_PROFILE`, `LAZYCLOUD_HOME` (default ~/.lazycloud/config.yaml), `LAZYCLOUD_ENDPOINT` (DOCS/cli/auth.mdx, DOCS/platform/auth.mdx)
  Delivered: lazycloud config resolution; config tests; docs/cli/auth.mdx unchanged.
- [x] Access tokens in the dashboard (Settings → Tokens): create with name and expiry (1–90 days or never), value shown once, list with last used/expires/status, delete, "Show device tokens" toggle (WEB/components/shared/SettingsDialog/AccessTokens/index.tsx, Q/tokens.ts)
  Intentional: AccessTokens/index.tsx, Q/tokens.ts. No admin-disable line, `expired` status: tasks/identity.md.
- [ ] Out-of-date client notice on every command, pointing to `lazycloud update` (CLI/main.py:180)
  Gap: the CLI reads `X-Lazycloud-Recommended-Client-Version`, but deployments never set `LAZYCLOUD_CLIENT_RELEASE_VERSION`, so the notice never shows. Handled by the deploy-packet (chart sets it from the release version).

## Workspaces, members and invitations
- [x] `lazycloud workspace list` (CLI/workspaces.py:72)
  Intentional: cli/workspaces.py; workspace CLI tests. `(deleting)` suffix and `--json` shape: tasks/identity.md.
- [x] `lazycloud workspace create NAME [--cloud aws]`: admin only, selects the new workspace, location fixed at creation (CLI/workspaces.py:87)
  Delivered: cli/workspaces.py; readiness checked server-side with the reference's messages (`8978b8bfa`).
- [x] `lazycloud workspace use NAME` (CLI/workspaces.py:127)
  Delivered: cli/workspaces.py; workspace CLI tests.
- [x] `lazycloud workspace rename NAME` renames the current workspace (CLI/workspaces.py:159)
  Intentional: cli/workspaces.py. Refusal without a selected workspace: tasks/identity.md.
- [x] `lazycloud workspace delete NAME [-y/--yes]`: admin only, asks for confirmation (CLI/workspaces.py:183)
  Intentional: cli/workspaces.py; `test_sdk_workspace_cli.py` confirmation tests. Returns once deletion has begun: tasks/identity.md.
- [x] Dashboard workspace switcher: switch (keeps current section), Create workspace (name + location LazyCloud or AWS), Rename, Members, Delete (WEB/components/shared/AppShell/WorkspaceSwitcher.tsx, CreateWorkspaceDialog.tsx, WEB/components/shared/WorkspaceRename/Dialog.tsx)
  Delivered: WorkspaceSwitcher.tsx, CreateWorkspaceDialog.tsx, WorkspaceRename/Dialog.tsx; smoke e2e switches workspaces.
- [x] Workspace deletion dialog (type the name, "Delete permanently" / "Keep workspace") and a "Deletion is incomplete → Resume deletion" recovery screen (WEB/components/shared/WorkspaceDeletion/index.tsx, WEB/routes/w/$workspace/route.tsx)
  Delivered: WorkspaceDeletion/index.tsx and the recovery screen; the screen also shows while an asynchronous deletion runs, and its copy fits.
- [x] "Workspace not found" page listing accessible workspaces (WEB/routes/w/$workspace/route.tsx)
  Delivered: routes/w/$workspace/route.tsx.
- [x] Members dialog: list members, invite by email with role (Administrator/Member), change role, remove member or leave, resend or revoke invitation (WEB/components/shared/AppShell/WorkspaceMembersDialog.tsx, Q/members.ts)
  Delivered: WorkspaceMembersDialog.tsx, Q/members.ts; `TestInvitations`, stack e2e invitations.
- [x] Invitation email: "You have been invited to <ws>", Accept button, expiry date, warning that the link joins whichever account opens it (R/packages/identity/src/identity/invitations.py:420)
  Delivered: internal/identity/invitations.go `invitationEmail`, same subject and body; `TestInvitations`. Real delivery unverified (no Resend credentials).
- [x] `/invitations/$token` page: preview, accept, decline, expired state (WEB/routes/invitations.$token.tsx)
  Delivered: routes/invitations.$token.tsx; stack e2e invitations.
- [x] Member limits by plan: Team allows 3, Business unlimited (DOCS/platform/plans.mdx)
  Delivered: internal/billing plan limits (Free 1, Team 3, Business unlimited); billing admission tests.

## Apps and deployments
- [x] `App(slug)`: lowercase letters, digits and underscores, starts with a letter, max 63 chars (SDK/abstractions/app.py:97)
  Delivered: shared/app_slug.py identical to the reference; openapi `AppName` checked by the request validator. Code reading only.
- [x] `App.deploy(prune=, resource=, workspace=, external_url=, source_root=)`: up to 4 resources deploy concurrently (SDK/abstractions/app.py:1025)
  Intentional: `test_deploy_uploads_the_source_once_and_maps_function_options`, `test_deploy_builds_each_distinct_image_once_and_sends_its_id`. `Deployment` return, no `external_url=`, one request per app: tasks/control.md.
- [x] `App.plan(prune=, workspace=)`, `App.deployment_manifest()`, `App.combine(apps)`, `App.resources` (SDK/abstractions/app.py:110-150)
  Intentional: app.py `plan`, `deployment_manifest`, `combine`, `resources`; `test_deploy_diff_previews_the_plan_without_deploying`. `DeploymentPlan` return: tasks/control.md.
- [x] `App.serve(resource=, timeout=, sync_dir=)` (SDK/abstractions/app.py:1126)
  Intentional: `TestServePreviewSyncsSourceAndStops`, `TestFunctionPreviewTakesTasksAndLapsesWithoutAFollower`, live serve (tasks/endpoints.md). `Preview` return: tasks/endpoints.md.
- [x] `lazycloud deploy HANDLER...`: module, module:object or file.py:object; several refs must all be complete apps (CLI/execution.py:65)
  Delivered: cli/execution.py `deploy`, cli/handler_workflows.py; `test_file_deploy_selects_the_whole_app_and_deduplicates_aliases`, `test_cli_deploy_of_a_file_deploys_its_app`; two apps live in /tmp/parity/live/twoapps.
- [x] `deploy --prune/-p` (complete apps only), `--diff/-d` (table App/Kind/Workload/Action/Existing versions with add/redeploy/retain/remove), `--workspace`, `--source-root` (CLI/execution.py:65-220)
  Intentional: `test_deploy_diff_previews_the_plan_without_deploying`, `TestPlanDeploymentActions`, `TestPruneWithoutFunctionsDeletesTheDeployedOnes`. `--diff --json` shape: tasks/control.md.
- [x] Deploy result card: "App deployed" (app, workloads, urls, devboxes, removed_versions) or "Deployment created" (name, version, url, role, keep_warm, preemptible) (CLI/execution.py:454)
  Delivered: execution.py `_emit_app_deployments`; `test_cli_deploy_of_a_file_deploys_its_app`, `TestDeployedFunctionsReportWhereTheyAnswer`. Live: the card lists the function and endpoint URLs.
- [x] Handler references: `module:object`, dotted package paths, `file.py:object`, bare module when it has exactly one app (DOCS/cli/overview.mdx)
  Delivered: references.py identical to the reference; test_sdk_handler_references.py, `test_file_deploy_requires_a_selection_when_several_apps_are_present`.
- [x] `lazycloud app list [--active|--inactive|--all]`, `app show APP`, `app pause APP`, `app resume APP`, `app delete APP`, by name or ID (CLI/apps.py:79-195)
  Intentional: cli/apps.py; `test_app_commands_accept_a_name_and_report_each_outcome`, `TestAppPauseResumeAndDeleteFreeTheName`. Columns: tasks/control.md.
- [ ] `lazycloud app export APP [-o/--output DIR] [--openapi res=file.json]... [--openapi-path res=/path]...`: typed package in `lazycloud_clients/<app>` (CLI/apps.py:24, SDK/client_codegen.py)
  Gap: functions export (test_app_export.py), but endpoints and ASGI apps are never exported and every `--openapi`/`--openapi-path` is refused, while docs/concepts/apps.mdx promises both. About 1-1.5 days.
- [x] `lazycloud deployment list [--app] [--limit 100]` (CLI/execution.py:352)
  Intentional: `test_deployment_references_resolve_names_and_versions`. Lists workloads, not versions: tasks/control.md.
- [x] `lazycloud deployment stop IDS_OR_NAMES...`, `start ID`, `scale ID --containers N` (pods only), `delete ID` (CLI/execution.py:377-450)
  Intentional: `test_deployment_references_resolve_names_and_versions`, `TestDeploymentStopStartVersionsAndDelete`, `TestPodsFollowConnectionsScalesAndParks`. Version-scoped stop/delete: tasks/control.md.
- [x] `Deployment` handle: `id`, `name`, `stub_id`, `invoke_url(port=, url_type=)`, `submit()`, `subscribe()` (SDK/session/deployment.py:181)
  Delivered: session/deployment.py `Deployment`; `test_deployment_handles_submit_to_the_active_version` (submit, `invoke_url()`, `url_type="stub"`, `port=` on pods only). Live on the parity stack for a function and an endpoint.
- [x] Each deploy records a new version; source is uploaded, not baked into the image; first upload writes `.lazycloudignore` (always excluded: .git .venv __pycache__ *.pyc .env .lazycloud/) (DOCS/concepts/workflow.mdx, SDK/source_sync.py:69)
  Intentional: `test_ignore_file_never_removes_the_baseline`, `test_ignore_file_uses_gitignore_semantics`, `test_deploy_uploads_the_source_once_and_maps_function_options`. Unchanged redeploys keep their version: tasks/control.md.
- [x] Where a call goes: a local preview first, then a fresh working-tree run from a laptop, then the deployed function when inside a container (DOCS/concepts/workflow.mdx)
  Delivered: function.py `_release_id`; `test_calls_inside_a_container_run_the_active_release_as_children`, `TestFunctionPreviewTakesTasksAndLapsesWithoutAFollower`, `TestStoppedWorkloadRunsUnchangedWorkingTreeCalls`.
- [x] Volumes, secrets and stored data survive app deletion and pruning (DOCS/concepts/apps.mdx)
  Delivered: soft-deleted apps, workspace-scoped volumes and secrets, `artifacts.app_id on delete set null` (migrations/0007_storage.sql). Schema reading only.

## Functions and tasks
- [ ] `@app.function(...)` options: image, name, cpu, memory, disk, gpu, gpu_count, timeout_seconds, concurrency, in_process, cron, keep_warm, max_pending_tasks, autoscaler, retries(3), retry_policy, retry_delay_seconds, callback_url, authorized, env, secrets, volumes, on_start/on_running/on_success/on_error/on_retry/on_failure/on_finish, task_policy, inputs, outputs, docker_enabled, preemptible, region, availability_zone, machine, metadata (SDK/abstractions/app.py:245)
  Gap: every option maps (`test_deploy_uploads_the_source_once_and_maps_function_options`, `test_deploy_maps_workload_runtime_options` incl. `metadata`, `test_deploy_maps_storage_options`, `test_deploy_maps_gpu_and_placement_options`; live metadata read back) except a non-default `retry_policy.retry_on_statuses`, which is refused: about half a day for a `retry_on` list of failure kinds.
- [x] `Function.local()`, plain call, `.remote()`, `.async_remote()` (SDK/abstractions/function.py:243,488,564)
  Delivered: `test_remote_streams_output_resumes_dropped_logs_and_returns_the_value`, `test_remote_failure_reraises_the_remote_exception` (original exception re-raised: tasks/function-execution.md). On a paused app deployed calls are refused and working-tree calls and previews run, as in the reference (`TestWorkingTreeReleaseRunsWhileTheDeploymentIsStopped`).
- [x] `.spawn()`, `.async_spawn()` return `FunctionCall`; `.spawn_map(inputs)` submits up to 8 at a time (SDK/abstractions/function.py:517,525,574)
  Intentional: `test_spawn_map_submits_in_batches_and_keeps_input_order`. Batched spawn_map: tasks/control.md.
- [x] `.map(inputs)` yields results in input order and `None` for a failed item (SDK/abstractions/function.py:578)
  Intentional: `test_spawn_map_submits_in_batches_and_keeps_input_order`, `test_ctrl_c_during_map_cancels_every_unfinished_task_despite_a_second_interrupt`. No client-side deadline: tasks/control.md.
- [x] `Function.deploy()`, `.serve()`, `.shell()`, `.prepare()`, `.spec()` (SDK/abstractions/function.py:333-487)
  Intentional: `test_standalone_shells_start_a_shell_instance_of_the_release`, `test_cli_deploy_of_a_file_deploys_its_app`, live serve (tasks/endpoints.md). Return types and dropped stub attributes: tasks/control.md.
- [x] `FunctionCall`: `task_id`, `task`, `result(wait=, timeout_seconds=)` giving TaskResult(ok, status, value, error, exit_code), `get()`, `logs()`, `output()`, `subscribe()`, `cancel()`, `rerun()`, `gather()` (SDK/session/task.py:250-355)
  Intentional: session/task.py; `test_task_handles_read_results_logs_and_reruns`. No exit code, no poll interval, `LogEntry`: tasks/control.md.
- [x] A pending FunctionCall passed as an argument arrives as its result (dependency tracking) (SDK/abstractions/function.py:786)
  Delivered: `test_function_calls_in_arguments_become_dependencies`, dependencies_test.go, runner `test_dependency_frames_resolve_upstream_results_for_the_next_invoke`, live nested spawn (tasks/control.md).
- [x] `Task.from_id(id, workspace=)`, `.get()`, `.view()`, `.pending_progress`, `.result()`, `.wait()`, `.async_wait()`, `.logs()`, `.output()`, `.subscribe()`, `.cancel()` (SDK/session/task.py:138)
  Intentional: `test_task_handles_read_results_logs_and_reruns`. API `Task` returns: tasks/control.md.
- [ ] `RetryPolicy(max_attempts, delay_seconds, backoff=RetryBackoff.Fixed|Exponential, max_delay_seconds, retry_on_statuses)`, `TaskPolicy(timeout_seconds)` (SH/tasks.py:30-70)
  Gap: fixed/exponential backoff and max delay work (internal/execution `NextAttemptDelay`), but a non-default `retry_on_statuses` is refused. About half a day.
- [x] `current_task_id()`, `current_root_task_id()` (SDK/__init__.py)
  Delivered: `test_calls_inside_a_container_run_the_active_release_as_children`, python/tests/acceptance/test_workload_runtime.py.
- [x] Arguments and results use cloudpickle from the SDK and JSON over HTTP, `--json` and exported clients; 16 MiB cap each (DOCS/concepts/functions.mdx)
  Delivered: session/task.py, `Function.submit_json`, internal/edge/invoke.go; 16 MiB cap in internal/execution admission and completion; `TestClaimCapsTotalInputBytes`.
- [x] Task lifecycle: pending, running, retry, then complete/failed/timeout/cancelled/expired (DOCS/concepts/tasks-and-logs.mdx)
  Intentional: Status words follow the API (`queued`, `running`, `succeeded`, `failed`, `cancelled`): tasks/control.md.
- [x] Pending reasons after 5s: queued, dependencies, retry, capacity_busy, capacity_unavailable, capacity_limit, provisioning_compute, starting_container (SDK/terminal.py `_pending_card`)
  Delivered: internal/execution/pending.go, same messages; task_views_test.go, capacity_controller_test.go, `test_remote_reports_why_a_queued_task_waits`, live `capacity_busy`.
- [x] `lazycloud.progress(callback)`, `TaskPendingProgress`, `TaskPendingReason`, `PendingProgressCallback` (SDK/progress.py)
  Intentional: progress.py; live callback run (tasks/control.md). No `for_reason`: tasks/control.md.
- [x] `lazycloud run HANDLER [ARGS]... [--workspace] [--output FILE.(png|html|txt|json|pkl)]`: JSON-parses args; pod handler runs an instance; plain callable runs locally (CLI/execution.py:223)
  Delivered: cli/execution.py `run`; `test_cli_run_json_and_task_commands_use_the_task_api`, `test_run_json_preserves_values_and_reports_unsupported_results_without_stdout`, live Ctrl-C cancel.
- [x] `lazycloud task list [--limit 100] [--app]`, `task show ID`, `task result ID [--wait/--no-wait] [--timeout] [--output]`, `task logs ID [--limit 250]`, `task stop IDS...`, `task cancel ID` (CLI/resources.py:441-606)
  Intentional: cli/tasks.py; test_cli_control.py. `task result` decodes the value locally: tasks/control.md.
- [x] Terminal steps for `.remote()`/`run`: one row per step with spinner, then ✓ or ✗, name padded to 11 chars, summary, elapsed shown as 1.2s/12s/1m 05s/1h 02m (SDK/terminal.py:53,386)
  Intentional: terminal.py; live cold `.remote()` rows (tasks/control.md). Last-8 short ids: tasks/control.md.
- [x] Step "Image": "preparing", then "python 3.12 · cached" or "python X · built"; build logs indented 4 spaces, last 3 lines live, last 20 kept on failure (SDK/abstractions/image.py:636,886)
  Delivered: `test_build_output_is_opt_in_and_uses_stderr`, `test_build_summary_follows_buildkit_steps_retries_and_dropped_streams`, `test_a_ready_image_is_cached_without_a_build_or_upload`.
- [x] Step "Source": "collecting files", then "syncing N files, X MB", then "syncing NN% · …", done as "N files, X MB" (SDK/source_sync.py:145-171)
  Delivered: session/deployment.py `_upload_source` with the reference strings; live cold run (tasks/control.md).
- [x] Step "Runtime": `<name>`, done as `<name> · <stub8>` (SDK/session/deployment.py:364)
  Intentional: `_runtime_done` writes `name · short_id(release)`; short ids: tasks/control.md.
- [x] Step "Task": "submitting", then `<task8> submitted`, then `<task8> <status>` (SDK/abstractions/function.py:690,736)
  Intentional: function.py `_remote_call`; short ids and API status words: tasks/control.md.
- [x] Remote stdout/stderr stream with a `│ ` rail prefix (stderr in the error color), lines flushed on newline, CR treated as newline (SDK/terminal.py:250,318)
  Delivered: terminal.py `remote_output` unchanged; `test_remote_streams_output_resumes_dropped_logs_and_returns_the_value`.
- [x] Pending card: "Task <id8> · pending <elapsed>", then the message, then "Next step  <hint>"; warning tone for capacity_unavailable/limit (SDK/terminal.py:330)
  Intentional: `test_remote_reports_why_a_queued_task_waits`; short ids: tasks/control.md.
- [x] Non-TTY, dumb terminal or `--json`: no live redraw, each log line printed as it arrives; all progress on stderr (SDK/terminal.py:363)
  Delivered: `test_output_channels_preserve_json_cleanliness_and_restore_human_state`.
- [x] Result on stdout: str printed raw, other values Rich-pretty-printed; "Saved <path>" with `--output`; rich display hint for images/HTML (CLI/components/results.py:20)
  Delivered: cli/components/results.py identical; `test_run_json_preserves_values_and_reports_unsupported_results_without_stdout`, test_sdk_cli_output.py.
- [x] `.map()`/`spawn_map()` show only the Image/Source/Runtime prepare steps once, with no per-item Task step; failures print "Task failed during map: …" (SDK/abstractions/function.py:578)
  Delivered: function.py `_invocation_session` prepares once and `map` prints "Task failed during map: …"; code reading.
- [x] `output(enabled=False)` silences display; calls inside containers are quiet unless wrapped in `with output():` (SDK/terminal.py:28)
  Delivered: terminal.py unchanged; code reading only.
- [x] Ctrl-C or a lost connection during `.remote()` cancels the task (SDK/abstractions/function.py:706)
  Intentional: `test_remote_cancels_the_task_when_following_ends_early`, `test_ctrl_c_during_submit_cancels_the_admitted_task`. 600 s reconnect window: tasks/control.md.

## Maps and queues
- [x] `Queue(name, workspace=)`: `put`, `pop` (None when empty), `peek`, `empty`, `len()`, `delete`; values JSON-serializable (SDK/abstractions/queue.py:37)
  Intentional: abstractions/queue.py; `test_queue_is_first_in_first_out` (live), `TestQueueDeliversEachMessageOnceInOrder`, `TestQueuePopWaitsForPut`. Empty message vs empty queue and the 1 MiB cap: tasks/storage.md.
- [x] `Map(name, workspace=)` as a MutableMapping: `[]`, `get`, `set(key, value, ttl=)` (max 7 days, 0 means no expiry), `in`, `keys`, `len`, `del`, `delete()` (SDK/abstractions/map.py:54)
  Intentional: abstractions/map.py; `test_map_behaves_like_a_mutable_mapping` (live), `TestMapCompareAndSet`, `TestMapExpiryKeysAndStats`. `KeyError` on a missing key and non-empty keys: tasks/storage.md.
- [x] Dashboard Storage → Queues: size, head message, Add message (JSON), Remove next message (consumes), delete queue (WEB/routes/w/$workspace/storage/-components/CollectionInspectors.tsx)
  Intentional: CollectionInspectors.tsx; e2e stack.spec.ts "a queue takes a message, shows its head and gives it up". No write rate: tasks/web.md.
- [x] Dashboard Storage → Maps: keys filtered by prefix, view value (binary Python values view/delete only), Edit value (JSON, expiry options, conflict check), Add key, Delete key, delete map, stats (stored/expiring/next expiry/writes) (WEB/routes/w/$workspace/storage/-components/CollectionInspectors.tsx, CollectionValueForm.tsx)
  Delivered: CollectionInspectors.tsx, CollectionValueForm.tsx; e2e "a map key is added, edited with a revision check and deleted", CollectionInspectors.test.tsx.

## Schedules
- [x] `cron=` on `@app.function`, always UTC: 5-field cron, `@hourly/@daily/@midnight/@weekly/@monthly/@yearly/@annually`, `every 5m` (max 59m/23h/31d); deploy rejects invalid values (DOCS/concepts/schedules.mdx)
  Delivered: internal/schedules/cron.go; `TestParseCronMatchesCroniter`, `TestDeployNormalizesAndReplacesTheSchedule`, live `every 1m` in test_workload_runtime.py.
- [x] Scheduled functions default to keep_warm=0; overlapping runs queue unless concurrency/autoscaler allow parallel runs (DOCS/concepts/schedules.mdx)
  Delivered: `scheduledKeepWarmSeconds = 0` asserted in `TestDeployNormalizesAndReplacesTheSchedule`; overlap queues under the default autoscaler (code reading).
- [x] Redeploying with a changed cron replaces the schedule; removing cron keeps the function callable (DOCS/concepts/schedules.mdx)
  Delivered: internal/schedules `Apply` in the deploy transaction; `TestDeployNormalizesAndReplacesTheSchedule`.
- [x] Dashboard workload page shows Schedule, Timezone, Last run, Next run (WEB/routes/w/$workspace/apps/-workloads/WorkloadOperation.tsx, Q/cron.ts)
  Delivered: WorkloadOperation.tsx `ScheduleFacts` over `WorkloadDetail.schedule`; code reading only.

## Endpoints, ASGI and realtime
- [ ] `@app.endpoint(...)`: route "/", methods GET+POST, domain, workers, concurrency, keep_warm 180, max_pending_tasks 100, timeout 180, retries 0, checkpoint_enabled, authorized True, plus function options (SDK/abstractions/app.py:454)
  Gap: signature, defaults and `metadata=` map (`test_deploy_maps_endpoint_and_asgi_options_to_http_specs`, `TestDeployEndpointResolvesHTTPDefaultsAndClaimsItsSubdomain`), but `callback_url=` fails the deploy as unsupported: requests are not tasks. Per-request callbacks: about 1 day, or record as intentional.
- [x] JSON body maps to function args; a returned Pydantic model is sent as JSON and becomes the response schema (DOCS/concepts/endpoints.mdx)
  Delivered: runner/http.py; `test_endpoint_maps_body_and_query_to_arguments_and_models_to_json`, `test_endpoint_results_map_to_responses`.
- [x] `Endpoint.request(*args)` returns EndpointResponse(status_code, text, json()); `.target("auto"|"deployed"|"served", deployment_name=, deployment_version=)` (SDK/abstractions/endpoint.py:407,419,187)
  Delivered: abstractions/endpoint.py, `resolve_url`; `test_endpoint_request_sends_the_token_only_to_an_authorized_deployment`.
- [x] `Endpoint.deploy()`, `.serve()`, `.shell()`, `.local()` (SDK/abstractions/endpoint.py:384-460)
  Intentional: `TestServePreviewSyncsSourceAndStops`, `test_standalone_shells_start_a_shell_instance_of_the_release`. Return types: tasks/control.md (`Deployment`), tasks/endpoints.md (`Preview`).
- [x] `app.asgi(name=, route=, domain=, workers=, concurrent_requests=, keep_warm_seconds=, max_pending_tasks=, authorized=, checkpoint_enabled=, …)(fastapi_app)` (SDK/abstractions/app.py:568)
  Delivered: app.py `asgi`; `test_deploy_maps_endpoint_and_asgi_options_to_http_specs`, `TestASGIStreamsUploadsUpgradesAndStripsTheToken`. `callback_url=` shares the endpoint gap.
- [x] `ASGI.request(method=, path=, json=, data=, headers=, params=, target=, deployment_name=, deployment_version=)` (SDK/abstractions/endpoint.py:780)
  Delivered: endpoint.py `ASGI.request`, same `resolve_url`/`send_request` as Endpoint; `TestWorkloadsAnswerOnTheAPIHostWithTheSession`.
- [x] `@app.realtime(name=, …)`: WebSocket handler, one call per message, an iterable return sends several messages (SDK/abstractions/app.py:663, SDK/abstractions/endpoint.py:841)
  Delivered: endpoint.py `RealtimeASGI`; runner `test_realtime_answers_each_message_and_iterables_send_several`, `TestASGIStreamsUploadsUpgradesAndStripsTheToken`.
- [x] Deployed URL `https://<name-stem>-<8hex digest>.<base>/<route>`; `-latest` means latest, `-vN` pins a version (SH/deployment_subdomains.py, SH/urls.py `build_deployment_url`)
  Delivered: internal/edge/urls.go; `TestSubdomainMatchesReference`, `TestPublicWorkloadsAnswerWithoutAToken` (`-latest`, `-v1`).
- [x] Stub URL `https://<stub_id>.<base>` and container URL `https://<container_id>.<base>` (SH/urls.py `build_stub_url`, `build_container_url`)
  Intentional: internal/edge/urls.go; `TestEndpointColdWarmAndScaleToZero`. Release host replaces the stub host: tasks/endpoints.md.
- [x] Host routing rewrites to `/api/v1/{functions|endpoints|asgi}/{public/<id>|id/<id>|<name>/latest|<name>/vN}`; unknown hosts fall through (R/apps/api/src/api/server/host_routing.py)
  Intentional: internal/edge/routes.go, internal/api/workload_routes.go; `TestWorkloadsAnswerOnTheAPIHostWithTheSession`. Unknown hosts 404 and the path routes: tasks/endpoints.md.
- [x] `Authorization: Bearer <token>` required unless `authorized=False`; 429 past max_pending_tasks (DOCS/concepts/endpoints.mdx)
  Delivered: internal/edge/handler.go; `TestEndpointQueuesPastCapacityAndRejectsPastMaxPending`, `TestPublicWorkloadsAnswerWithoutAToken`.
- [x] Functions are also HTTP-invokable: POST `/api/v1/functions/{name}/latest`, `/v{N}`, `/id/{stub}`, `/public/{stub}`, `/invoke/stream` NDJSON (R/apps/api/src/api/server/routers/functions.py)
  Intentional: openapi `invokeFunction`/`invokeFunctionVersion`; `TestFunctionsAreInvokedOverHTTP`. No NDJSON stream, `{task, result}` answer, open hosts instead of `/public/`: tasks/endpoints.md.
- [x] WebSocket routes for ASGI/realtime by id/public/latest/version (R/apps/api/src/api/server/routers/endpoints.py:333-440)
  Intentional: edge upgrades on every host form; `TestASGIStreamsUploadsUpgradesAndStripsTheToken`, `TestRelayCarriesUpgradesAndBrokenResponses`. Path routes: tasks/endpoints.md.

## Pods and devboxes
- [x] `app.pod(name, image, command, ports={"http":8080}, env, cpu 1.0, memory 128Mi, disk, gpu, keep_warm 600 (-1 = always on), secrets, volumes, disks, authorized False, checkpoint_*, health_check_path/port, tcp, ssh, block_network, allow_list, docker_enabled, preemptible, region, availability_zone, machine, metadata)` (SDK/abstractions/app.py:755)
  Delivered: signature matches; `TestPodDefinitionsResolveTheirDefaults`, `TestPodDefinitionsRejectWhatTheyCannotRun`, live `test_a_pod_deploys_answers_on_its_url_scales_and_starts_instances`; `metadata=` is stored with the release.
- [x] `Pod.create(command=, timeout_seconds=)` returns PodInstance(url, terminate()); `Pod.run(*cmd)` (SDK/abstractions/pod.py:375,395,113)
  Delivered: pod.py `create`/`run`, `PodInstance.terminate`; `test_pod_instances_start_from_the_prepared_release_and_terminate`, live pod test.
- [x] `Pod.deploy()`, `.pause()`, `.resume()`, `.scale(containers)`, `.delete()` (optional version), `.shell()` (SDK/abstractions/pod.py:407-520)
  Intentional: pod.py via DeploymentClient; `test_pod_lifecycle_resolves_its_deployment_by_app_and_name`. `Deployment` return and version-scoped stop/delete: tasks/control.md.
- [x] `Container.attach(container_id=, sync_dir=, hide_logs=)` (SDK/abstractions/pod.py:133)
  Intentional: pod.py `attach`; `test_container_attach_follows_a_pod_command_to_its_exit_code`, `TestPodCommandExitReportsItsCode`. `ContainerAttachment` and `workspace=`: tasks/workloads.md.
- [x] Pod URL `https://<stub_id>-<port>.<base>` or `<container_id>-<port>.<base>` (SH/urls.py `build_pod_url`, `pod_proxy_url`)
  Delivered: internal/edge; `test_pod_and_sandbox_ports_are_addressed_by_hostname`, live pod test.
- [x] TCP pod `tls://<stub>-<port>.<tcp host>` with SNI; always public (SH/urls.py:12, DOCS/concepts/pods.mdx)
  Delivered: internal/edge/tcp.go; `TestPodDefinitionsRejectWhatTheyCannotRun`, `TestTCPConnectionsAreBoundedPerTarget`, live TLS client got 200 (tasks/workloads.md).
- [x] `app.devbox(name, image, disk, cpu, memory, agent_harnesses=all, gpu, keep_warm (30min default), preemptible False, command, ports, env, secrets, volumes, disks, docker_enabled, region, machine)` (SDK/abstractions/app.py:860)
  Delivered: app.py `devbox`; `TestPodDefinitionsResolveTheirDefaults`, `test_app_deploy_sends_pods_and_devboxes_with_their_defaults_and_never_sandboxes`, live `test_ssh_config_makes_plain_ssh_reach_a_devbox`.
- [x] `AgentHarness` Codex/Claude/OpenCode/Pi installed at build together with Node 22 and git (SDK/agent_harness.py, DOCS/concepts/dev-machines.mdx)
  Delivered: agent_harness.py identical to the reference; code reading only, no build with harnesses has run.
- [x] `lazycloud devbox list [--app] [--workspace] [--limit] [--cursor]` prints "Next page: --cursor …" (CLI/devbox.py:62)
  Delivered: cli/devbox.py; `test_devbox_list_pages_and_status_reports_the_devbox`.
- [x] `lazycloud devbox NAME status` card: phase, cpu, memory, disk, connections, phase reason (CLI/devbox.py:132)
  Delivered: cli/devbox.py; `test_devbox_list_pages_and_status_reports_the_devbox`, live devbox run.
- [x] `lazycloud devbox NAME ssh [--app] [-- ssh args]` (CLI/devbox.py:115)
  Delivered: cli/devbox.py, session/ssh.py; live `devbox <name> ssh` in `test_ssh_config_makes_plain_ssh_reach_a_devbox`.
- [x] `lazycloud devbox NAME login codex|claude|opencode|pi [--app]`: browser login with SSH callback forwarding, no `--json` (CLI/devbox.py:91, SDK/session/agent_login.py)
  Delivered: session/agent_login.py identical to the reference; forwarding covered by `TestSSHSessionsExecShellSignalSFTPAndForwarding`; no live login run.
- [x] Devbox wake phases in the connect spinner: waking up, waiting for a machine, pulling image, restoring disk, starting, connecting, saving disk (CLI/ssh.py:97)
  Delivered: cli/ssh.py phases, internal/execution/devbox.go `livePhase`; `TestPodsFollowConnectionsScalesAndParks`.
- [x] Dashboard pod page: Instances list (state/uptime, All/Active filter), replica stepper + Apply, instance drawer with compute, placement and terminal access (WEB/routes/w/$workspace/apps/-workloads/PodInstances.tsx, PodInstanceDrawer.tsx)
  Delivered: PodInstances.tsx, PodInstanceDrawer.tsx; `-workload-route.test.tsx`; no stack journey.
- [x] Dashboard devbox page: Status, Connections, Disk, Idle stop, SSH config host, Start/Stop, Start logs, Files browser, Metrics, Versions (WEB/routes/w/$workspace/apps/-workloads/DevboxDetail.tsx, Q/deployments.ts)
  Delivered: DevboxDetail.tsx; `-workload-route.test.tsx`; no stack journey (devbox root disk needs nbd-client: tasks/web.md).

## Sandboxes
- [x] `app.sandbox(cpu 1.0, memory 128 (MiB), disk, gpu, gpu_count, image, keep_warm_seconds 600, authorized, name, volumes, secrets, env, sync_local_dir, block_network, allow_list, docker_enabled, preemptible, ports, region, availability_zone, machine, metadata, command)` (SDK/abstractions/app.py:949)
  Delivered: signature matches; `test_sandbox_create_prepares_an_empty_workspace_waits_and_exposes_its_ports`, live `test_a_sandbox_runs_processes_and_files_and_controls_its_network`; `metadata=` is stored with the release.
- [x] `Sandbox.create()`, `.create_from_memory_snapshot(id)`, `.connect(sandbox_id)`, `.list(app_id=, limit=50)`, `.stats()`, `.timeline(stub_id, container_id=)` (SDK/abstractions/sandbox.py:1756-1840)
  Intentional: `test_sandbox_listing_stats_and_timeline`, `TestInstancesStopOnceIdleAndConnectionsKeepThemUp`, `TestSnapshotsAreReportedOnceByTheirHost`. One row per sandbox container: tasks/workloads.md.
- [x] `SandboxInstance`: `id`/`sandbox_id()`, `run(cmd, timeout_seconds=, cwd="/workspace", env=)`, `expose_port`, `list_urls`, `network_permissions`, `update_network_permissions(block_network=, allow_list=)`, `update_ttl`, `snapshot_memory`, `create_image_from_filesystem`, `list_processes`, `terminate` (SDK/abstractions/sandbox.py:1374)
  Intentional: `test_sandbox_ports_network_lifetime_snapshots_and_termination`, live sandbox test. Idle-lifetime TTL: tasks/workloads.md; memory snapshots need a root agent (Gaps there).
- [x] `instance.process`: `run`, `run_code(code, blocking=)`, `exec(*argv)`, `list_processes`; SandboxProcess `pid`, `status()`, `kill()`, `wait()`, `result()`, and `stdout`/`stderr`/`logs` streams with `read()`/`lines()` (SDK/abstractions/sandbox.py:416,549)
  Delivered: sandbox.py process manager; `test_sandbox_processes_start_poll_stream_and_kill`, live exec/kill/exit codes.
- [x] `instance.fs`: `upload_file`, `download_file`, `stat_file`, `list_files`, `create_directory`, `delete_directory`, `delete_file`, `find_in_files`, `replace_in_files` (SDK/abstractions/sandbox.py:686)
  Intentional: `test_sandbox_file_operations_map_to_the_container_file_routes`, live round trip. 64 MiB per request, every match: tasks/workloads.md.
- [x] `instance.docker`: run, build, pull, push, tag, ps, images, logs, stop, rm, rmi, exec, login, compose_up/down/logs/ps/build, volume_create/ls/rm (SDK/abstractions/sandbox.py:925)
  Intentional: SDK docker manager identical to the reference; `TestDockerPodRunsADaemon`. No Docker with network policy: tasks/workloads.md.
- [x] Async `.aio` twins for instance, process, fs and docker (SDK/abstractions/sandbox.py:1522)
  Delivered: same `to_thread` wrappers as the reference; code reading only.
- [x] Error types: SandboxConnectionError, SandboxProcessError, SandboxProcessTimeoutError, SandboxFileSystemError; data types SandboxFileInfo, SandboxFileSearch* (SDK/__init__.py)
  Delivered: same exports; `test_a_sandbox_that_stops_while_starting_is_terminated_and_reported`, file-ops test.
- [x] Dashboard app page Sandboxes section (WEB/routes/w/$workspace/apps/-components/AppSandboxesSection.tsx)
  Delivered: AppSandboxesSection.tsx over `/sandboxes` and `/sandboxes/stats`; code reading only.
- [x] Sandbox page: Terminal, Files (upload/download/delete), Processes (PID/command, Stop process), Network (ports/URLs), Lifecycle, Lineage, Save image, Snapshot memory (WEB/routes/w/$workspace/sandboxes/$containerId.tsx, Q/sandboxes.ts)
  Intentional: sandboxes/$containerId.tsx with every tab and action; code reading only. Ready-gated snapshot buttons and published ports only: tasks/web.md.

## Shells and SSH
- [x] `lazycloud shell HANDLER [--sync-dir/--sync DIR] [--workspace]` or `shell --container-id ID`; rejects `--json`; "Connecting <target>" step (CLI/execution.py:265, CLI/components/progress.py:66)
  Delivered: cli/execution.py; test_public_cli.py (`--container-id`, `test_interactive_shell_rejects_json_output_before_creating_a_session`), live standalone shell.
- [x] `lazycloud dev [HANDLER] [--sync ./] [--workspace]`: default dev pod uses the managed image (CLI/development.py:20)
  Delivered: cli/development.py identical to the reference; code reading only.
- [x] `lazycloud ssh NAME [--app] [--workspace] [-- ssh args]` for a devbox or an `ssh=True` pod (CLI/ssh.py:38)
  Delivered: cli/ssh.py; `test_ssh_names_why_a_pod_cannot_be_reached`, `TestSSHCertificatesAreSignedByTheWorkspaceAuthority`, live `lazycloud ssh`.
- [x] `lazycloud ssh-config [NAMES...] [--prune/-p] [--app] [--workspace]` writes ~/.lazycloud/ssh/config with hosts `lazycloud-<ws>-<app>-<name>` and includes it from ~/.ssh/config (CLI/ssh.py:157)
  Delivered: cli/ssh.py; `test_ssh_config_writes_hosts_a_certificate_and_the_user_include`, test_ssh_config.py, live `ssh -F`.
- [x] Hidden `ssh-proxy POD --app` (stdio tunnel used as ProxyCommand) and `ssh-cert [--quiet] [--force]` (short-lived certificate) (CLI/ssh.py:64,135)
  Intentional: `test_ssh_runs_the_hidden_commands_of_the_running_cli`, live ssh runs. Interpreter-pinned commands: tasks/workloads.md.
- [x] `Shell`/`ShellSession` SDK (create_standalone, create_existing, connect) (SDK/abstractions/shell.py)
  Intentional: abstractions/shell.py; `test_shell_waits_for_its_container_and_bridges_bytes_resizes_and_exit`, `TestShellRunsALoginShellOnAPTY`. Protocol without username/password: tasks/workloads.md.
- [x] Dashboard container shell dialog over WebSocket `/api/v1/shells/id/{stub}/{container}/ws` (WEB/components/shared/ShellDialog/index.tsx, Q/shells.ts)
  Intentional: ShellDialog, Terminal; `/v1/workspaces/{ws}/containers/{c}/shell` with session and Origin: tasks/workloads.md. No test opens the dashboard socket.

## Images
- [x] `Image(python_version="3.12", python_packages, commands, base_image, base_image_creds, env_vars, image_id, architecture=LinuxArchitecture.Amd64)`; Python 3.10–3.14 or an exact patch release (SDK/abstractions/image.py:145, SH/image_building/authoring.py:21)
  Intentional: `test_default_image_sends_no_base_and_versions_normalize`, `test_definition_maps_every_authoring_option`. `python:<version>-slim` base: tasks/images.md.
- [x] Factories `from_registry(uri, credentials=, python_version=)`, `from_dockerfile(path, context_dir=)`, `from_id(id)` (SDK/abstractions/image.py:200-216)
  Intentional: e2e private `from_registry`, `test_from_id_reads_the_image_and_fails_for_an_unknown_id`, `TestDockerfilesCannotNameUncheckedImages`. Dockerfile and base-registry rules: tasks/images.md.
- [x] Project factories `from_uv(dir, extras, groups)`, `from_poetry`, `from_pyproject`, `from_micromamba(environment.yml)`, all with base_image, creds and architecture (SDK/abstractions/image.py:220-340)
  Delivered: e2e `from_uv`; parity sweep built from_poetry, from_pyproject and from_micromamba live (tasks/images.md); `test_micromamba_images_send_the_python_release_and_their_steps`.
- [ ] Builders `add_commands`, `add_python_packages` (list or requirements path), `add_micromamba_packages`, `with_envs(clear=)`, `add_local_path(pattern)`, `with_secrets`, `build_with_gpu(hint)`, `with_docker` (SDK/abstractions/image.py:344-426)
  Gap: every builder maps (`test_definition_maps_every_authoring_option`), but `with_secrets` and `build_with_gpu` fail as unsupported (tasks/images.md Gaps). `with_secrets`: about 1 day; GPU builds need GPU build capacity (days).
- [x] `Image.verify()`, `.exists()`, `.build()`, `.spec()`, `.get_credentials_from_env()` (SDK/abstractions/image.py:428-540)
  Intentional: `test_verify_reports_rejected_definitions_instead_of_raising`, `test_a_ready_image_is_cached_without_a_build_or_upload`, `test_named_credentials_come_from_the_environment`. `ImageVerification`: tasks/images.md; `build(machine=)` stays a gap there.
- [x] Private registry credential names per registry: GHCR, ECR, GCR/pkg.dev, ACR, NGC, Docker Hub (DOCS/concepts/images.mdx)
  Delivered: internal/images/credentials.go; `TestRegistryCredentialNames`, e2e private registry. ECR/GCR/ACR/NGC logins unverified live (tasks/images.md).
- [x] Content-addressed build cache; code changes never trigger a rebuild (DOCS/concepts/images.mdx)
  Delivered: `TestEqualDefinitionsShareOneImageAuthorizedPerWorkspace`, `TestConcurrentBuildRequestsJoinOneBuild`, e2e `from_uv` code edit shows `cached`.

## Secrets
- [x] `Secret(name, workspace=)`: `create(value)` (no overwrite), `update`, `set` (upsert), `get()`, `record()`, `delete()` (SDK/abstractions/secret.py:41)
  Delivered: abstractions/secret.py; `test_secret_lifecycle_and_errors`, `Secret.get()` from a container in test_workload_runtime.py.
- [x] Workloads receive listed `secrets=[...]` as environment variables (DOCS/concepts/secrets.mdx)
  Delivered: test_workload_runtime.py, `TestStartCarriesNamedSecretsAndFailsWithoutThem`, `TestSupervisorRedactsSecretsFromOutputAndFailures`.
- [x] `lazycloud secret list`, `create NAME VALUE`, `modify NAME VALUE`, `show NAME [--reveal]`, `delete NAME` (CLI/secrets.py:25-117)
  Intentional: cli/secrets.py; `test_cli_masks_values_unless_revealed_and_lists_every_page`. No `id`, value fetched only with `--reveal`: tasks/workload-runtime.md.
- [x] Dashboard Storage → Secrets: create (SECRET_NAME + value), Rotate, Delete, values masked (WEB/routes/w/$workspace/storage/-components/SecretsTab.tsx)
  Delivered: SecretsTab.tsx; e2e "a secret is created masked, revealed on request and deleted".

## Volumes and disks
- [x] `Volume(name, mount_path)`: `create`, `get_or_create`, `put`, `get`, `read_text`, `read_bytes`, `write_text`, `write_bytes`, `list`, `list_path`, `stat`, `move`, `remove`, `delete` (SDK/abstractions/volume.py:275-390)
  Intentional: abstractions/volume.py; live `test_volume_files_round_trip` (all 14 methods), `TestVolumeFiles`. API model returns: tasks/storage.md.
- [x] Volume presigned URLs and multipart upload: `presigned_url`, `create_multipart_upload`, `complete_multipart_upload`, `abort_multipart_upload`, `file_service_info` (SDK/abstractions/volume.py:394-480)
  Intentional: `TestVolumeMultipartUpload`, `TestWriteURLsAreShortLived`, live `test_volume_put_sends_large_files_in_parts`. One-hour write URLs and constant `file_service_info`: tasks/storage.md.
- [x] `CloudBucket(name, mount_path, CloudBucketConfig(access_key, secret_key, region, bucket, prefix, endpoint, force_path_style, read_only))` (SDK/abstractions/volume.py:58,252)
  Intentional: `TestCloudBucketKeysComeFromWorkspaceSecrets`, `TestCloudBucketMountsWithItsKeys`. Both key secrets required: tasks/storage.md.
- [x] `lazycloud volume list`, `volume create NAME`, `volume delete NAME [-y]` (CLI/volumes.py:25-85)
  Intentional: cli/volumes.py; live `test_volume_cli_commands`, `test_volume_delete_without_tty_requires_yes_flag`. Columns: tasks/storage.md.
- [x] File commands `lazycloud ls|cp|rm|mv` with `lazycloud://vol/path` (scheme needed only on a download source; cp uploads globs) (CLI/volumes.py:89-215)
  Delivered: cli/volumes.py; live `test_volume_cli_commands`, `test_volume_remote_path_parser_supports_plain_and_scheme_syntax`.
- [x] `Disk(name, size="50Gi", mount_path="/")`: `Disk.list()`, `.delete()`, `.mount()`; sizes 1Gi–1Ti, grow only (SDK/abstractions/disk.py:23)
  Intentional: `test_disk_sizes_are_bounded_whole_blocks`, live `test_disks_list_and_refuse_deleting_a_missing_disk`, `TestDiskLeaseFencesHolders`. Whole 4096-byte blocks: tasks/storage.md.
- [x] `lazycloud disk list`, `disk delete NAME [-y]` (CLI/disks.py:19,52)
  Delivered: cli/disks.py; live `test_disks_list_and_refuse_deleting_a_missing_disk`.
- [x] Dashboard Storage → Volumes: create, browse, upload, download, delete path, delete volume, "Used by" links (WEB/routes/w/$workspace/storage/-components/VolumesTab.tsx)
  Intentional: VolumesTab.tsx; e2e "a volume is created, a file uploaded, listed, downloaded and removed". Directories show no time: tasks/web.md.
- [x] Dashboard Storage → Disks: size, stored, status, used by, delete (WEB/routes/w/$workspace/storage/-components/DisksTab.tsx)
  Intentional: DisksTab.tsx; code reading only. No Deleting status; a devbox's disk reads "pod in APP": tasks/storage.md.

## Artifacts
- [x] `Artifact.file(path, content_type=, task_id=)`, `from_file(handle, suffix=)`, `from_pil_image(img, format=)`, `Artifact(path=, content_type=)` (SDK/abstractions/artifact.py:92-180)
  Delivered: abstractions/artifact.py; live `test_artifacts_save_for_a_task`.
- [x] `save(target_dir=, task_id=)` returns SavedArtifact(artifact_id, task_id, filename, expires_at); directories are zipped (SDK/abstractions/artifact.py:244)
  Delivered: live `test_artifacts_save_for_a_task` (zip, multipart, expiry), `test_tasks_use_queues_maps_and_artifacts_through_the_container_api`, `TestArtifactLifecycle`.
- [x] `public_url(expires=3600)`, `exists()`, `stat()`, `delete()`, `zip_dir()`, `package()`, `save_remote()` (SDK/abstractions/artifact.py:200-300)
  Intentional: live `test_artifacts_save_for_a_task`. Missing-id delete is `not_found`, `public_url` capped at retention: tasks/storage.md.
- [x] Retention by plan: Free 1 day, Team 30, Business 90 (DOCS/concepts/artifacts.mdx)
  Delivered: `Storage.ArtifactRetention` asks billing; `TestRetentionFollowsThePlan`.
- [x] `lazycloud artifact list [--workspace] [--task-id] [--search] [--cursor]`, `artifact usage`, `artifact delete ID [-y]` (CLI/artifacts.py:25-62)
  Intentional: cli/artifacts.py; live `test_artifacts_save_for_a_task`. `--json` shape: tasks/storage.md.
- [x] Dashboard Storage → Artifacts: search, filters (app/type/task/saved before/from), bulk select delete, preview, download, View task, refresh, retention column; storage usage summary (WEB/components/shared/Artifacts/index.tsx, ArtifactRow.tsx, ArtifactPreview.tsx)
  Intentional: components/shared/Artifacts/*; e2e "an artifact a task saved is previewed from the task and listed in storage". One-request bulk delete, no deleting states: tasks/storage.md.

## Custom domains
- [x] `lazycloud domain add HOSTNAME` prints phase, CNAME and ownership records, plus a hint to run `domain status` (CLI/domains.py:76)
  Delivered: cli/domains.py; live Cloudflare acceptance 2026-10-01 in tasks/endpoints.md.
- [x] `lazycloud domain status HOSTNAME` (single read, no waiting), `domain list`, `domain remove HOSTNAME` (CLI/domains.py:103-147)
  Delivered: cli/domains.py; same live acceptance (status, list, remove).
- [x] `domain="…"` on endpoint/asgi/realtime must be the registered name or a subdomain of it; Team/Business only (DOCS/platform/domains.mdx)
  Intentional: `TestDeployCustomDomainNeedsAnOwnerRegistrationAndOneDeployment`, `TestDomainRegistrationFollowsThePlan`. Exact registered name only: tasks/endpoints.md.
- [x] Custom hostname routing (R/apps/api/src/api/server/host_routing.py `_custom_hostname_target`)
  Intentional: internal/edge/routes.go; `TestCustomHostnameServesOnlyThroughTheWorkspaceOwnersRegistration`. Only ready domains route: tasks/endpoints.md.
- [x] Dashboard Settings → Domains: add, DNS record table (Type/Name/Target/Value), status, remove, "Upgrade to Team" gate (WEB/components/shared/SettingsDialog/DomainSettings.tsx)
  Delivered: DomainSettings.tsx, same records table, status, remove and Team gate; code reading only, no test.

## Compute (managed, AWS connect, joined machines)
- [x] `lazycloud compute status`, `compute instances`, `compute workloads` (CLI/resources.py:63-130)
  Intentional: `test_compute_status_summarizes_the_workspace`, `test_compute_instances_and_workloads_follow_every_page`. `--json` shapes: tasks/compute.md.
- [x] `lazycloud cloud connect aws --account-id [--role-arn] [--networks-json]` (CLI/resources.py:131)
  Delivered: `test_cloud_connect_returns_the_authorization_to_complete`; private stack run (tasks/compute.md).
- [x] `lazycloud cloud authorize [--profile AWS_PROFILE]` submits a CloudFormation stack from the local machine (CLI/resources.py:207)
  Intentional: `test_cloud_authorize_submits_the_stack_with_the_customer_profile`; template validated by AWS. Node role in the stack, us-east-2: tasks/compute.md. No real account run.
- [x] `cloud validate`, `cloud status [--watch --until PHASE --interval 2 --timeout 600]`, `cloud reconnect [--role-arn]`, `cloud cancel-reconnect`, `cloud retry`, `cloud disconnect [--open/--no-open] [--wait/--no-wait] [--interval] [--timeout]` (CLI/resources.py:176-410)
  Delivered: `test_cloud_validate_reports_failure_after_printing`, `test_cloud_status_shows_and_waits_for_a_phase`, `test_cloud_reconnect_cancel_and_retry`, `test_cloud_disconnect_waits_for_removal`; real STS `assume_role_denied`.
- [x] `lazycloud machine join --name --workspaces a,b [--gpu] [--max-cpu] [--max-memory] [--max-gpus|--gpu-ids] [--background/--foreground] [--service-manager] [--service-name] [--state-dir]` (CLI/resources.py:754, CLI/machine_join.py)
  Intentional: `test_machine_join_runs_the_command_with_the_agent_flags`, `TestInstallScriptInstallsAReleaseAndRunsTheAgent`, foreground join to ready in 1.73 s. `--server` and no Docker install: tasks/compute.md.
- [x] `lazycloud machine list`, `machine update NAME --workspaces`, `machine remove ID` (CLI/resources.py:714-868)
  Intentional: `test_machine_list_update_and_remove`; live remove. Only the joining account may update or remove: tasks/compute.md.
- [x] Machine phases: requested, provisioning, booting, joining, ready, draining, terminating, deleted, failed; offline badge (DOCS/platform/compute.mdx, WEB/lib/machine-lifecycle.ts)
  Delivered: openapi `MachineLifecycle`, web/src/lib/machine-lifecycle.ts; docs/platform/compute.mdx unchanged.
- [x] Placement options `machine=`, `region=` (us-east/us-west/eu-central/eu-north/ap-southeast), `availability_zone=`, `preemptible=` (DOCS/concepts/resources.mdx)
  Intentional: `test_deploy_maps_gpu_and_placement_options`, `test_invalid_placement_options_fail_where_declared`, `TestRunsPinnedToAnUnavailableMachineFailAtOnce`. Only US regions get capacity: tasks/compute.md.
- [x] GPU types `GpuType.T4/A10G/L4/L40S/A100_40/A100_80/H100/H200/Any`, preference lists; plain A100 rejected (SH/gpu.py, DOCS/concepts/resources.mdx)
  Delivered: shared/gpu.py identical; `test_invalid_placement_options_fail_where_declared`, `TestAgentGivesContainersFreeGPUs`, live RTX 3090.
- [x] `Autoscaler(min_containers=0, max_containers=1, tasks_per_container=1)` (SH/autoscaling.py)
  Delivered: `TestPlanScalesWithDemandWithinLimits`, `TestConcurrentPlannersRespectMaxContainers`, `TestIdleDrainRespectsKeepWarmAndRunningAttempts`.
- [x] Agent install endpoints `/install/agent` and `/install/agent/{os}/{arch}` (R/apps/api/src/api/server/routers/install.py)
  Delivered: internal/api/install.go; `TestAgentInstallsServePublishedReleaseArchives`, private-stack join.
- [x] Dashboard Settings → Compute: Connected clouds (Add cloud, AWS details, instances, "Upgrade to Business" gate), Self-hosted machines list, edit machine workspaces (WEB/components/shared/SettingsDialog/ComputeSettings.tsx, MachineWorkspaces.tsx)
  Delivered: ComputeSettings.tsx, MachineWorkspaces.tsx; gate enforced by `billing.AdmitConnectedCloud`. Code reading only.
- [x] AWS connection dialog: account ID, Continue to AWS, Check authorization, Reconnect, Cancel reconnect, Retry, Remove; last validated (WEB/components/shared/SettingsDialog/AwsConnectionDialog/index.tsx)
  Delivered: AwsConnectionDialog/*; controller.test.ts.
- [x] Join machine dialog: name + workspaces, Generate install command, host requirements, live connection status (WEB/components/shared/SettingsDialog/JoinMachineDialog.tsx)
  Delivered: JoinMachineDialog.tsx; status follows the returned machine id. Code reading only.

## Billing and plans
- [ ] Plans Free/Team/Business: prepaid credit balance, trial and subscription credit, per-second compute (DOCS/platform/plans.mdx)
  Gap: internal/billing/ratecard.go, same terms; `TestTrialCoversUsageAndNewCreditPaysDebtFirst`, `TestSubscriptionCreditIsSpentBeforePurchasedCredit`, `TestStripeTestMode`. CPU and memory bill at the reservation only, while docs/platform/plans.mdx says the greater of reservation and measured use (tasks/billing.md Gaps): about 1 day.
- [x] Placement multipliers: preemptible=False 3× CPU/memory; pinned region 1.5× (DOCS/platform/plans.mdx)
  Delivered: ratecard.go `placements()`; `TestContainersPriceTheirGPUPlacementAndMachine`.
- [x] Limits: concurrency, zero balance stops work, monthly usage limit resets on the 1st, 30-day unfunded retention (DOCS/platform/plans.mdx)
  Delivered: `TestAdmitRefusesWorkTheAccountCannotPayFor`, `TestAdmitCapsContainersAtTheAccountsConcurrency`, `TestUnfundedAccountsStopTheirContainers`, `TestUnfundedAccountsKeepTheirDataThirtyDaysAndAreWarned`, `TestUnfundedWorkspacesStoreNothingNewButReadWhatTheyHave`.
- [x] Email: "Add credit within 30 days to keep your stored data" (R/packages/storage/src/storage/unfunded_retention.py:104)
  Delivered: billing/retention.go `warnUnfunded`, same words; `TestUnfundedAccountsKeepTheirDataThirtyDaysAndAreWarned`.
- [x] Settings → Billing, Plan and payment: current plan, Change plan dialog (proration, scheduled downgrade, cancel scheduled change), add/update payment method, Invoices portal, past-due state (WEB/components/shared/SettingsDialog/BillingSettings/index.tsx, PlanDialog.tsx)
  Delivered: BillingSettings/*; controller.test.ts, `TestSubscriptionsSetTheAccountsPlan`, `TestPlanChangesCannotGoBelowHeldLimits`, `TestStripeTestMode`.
- [x] Prepaid credit: available balance, Add credits with preset or custom amount (Stripe checkout) (WEB/components/shared/SettingsDialog/BillingSettings/PrepaidCredit.tsx, AmountSelect.tsx)
  Delivered: PrepaidCredit.tsx, AmountSelect.tsx; `TestPaymentOutcomesFundOnceAndTakeBackReversals`, `TestStripeTestMode`. Completing hosted Checkout unverified.
- [x] Spending controls: monthly usage limit, automatic reload (threshold + amount), resume reload after a declined payment (WEB/components/shared/SettingsDialog/BillingSettings/BillingPreferences.tsx, Q/billing.ts)
  Delivered: BillingPreferences.tsx; preferences.test.tsx, `TestReloadDecidesFromASettledBalanceOnce`, `TestAPaidReloadWhoseResponseWasLostIsFoundByItsWebhook`.
- [x] Credit prompt in the app shell (WEB/components/shared/AppShell/CreditPrompt.tsx)
  Delivered: CreditPrompt.tsx, same threshold and copy; code reading only.
- [x] Public `/pricing` page and pricing catalog `/api/v1/pricing` (WEB/routes/pricing.lazy.tsx, Q/pricing.ts)
  Intentional: pricing.lazy.tsx; internal/api/billing_test.go catalog test. `/v1/pricing` path: tasks/billing.md.
- [ ] Usage page: range control, usage cost, amount covered by subscription credits, spend chart by category, breakdown by app and workload with runtime and cost, "View run", image builds, usage without an app (WEB/routes/w/$workspace/usage/index.tsx and -components/*)
  Gap: usage routes match; `TestStorageIsMeteredAndShownOnTheUsagePage`. Usage is metered per container, so runs have no rows, cost or "View run" (tasks/billing.md Gaps): about 1-1.5 days.
- [x] Stripe webhook `/webhooks/stripe` (R/apps/api/src/api/server/routers/webhooks.py:63)
  Delivered: internal/api/webhooks.go; `TestWebhooksAreVerifiedAndStoredOnce`, `TestStripeTestMode`.

## Logs, events and metrics
- [x] `lazycloud logs (--deployment|--task-id|--container-id) [-n/--lines 250 (1–1000)] [--show-timestamp] [-f/--follow] [--max-events 0]`; lines optionally `[ISO-ts] msg`; `--follow --json` prints NDJSON; empty result prints "No log entries found." (CLI/logs.py:16)
  Intentional: cli/logs.py; `test_logs_needs_one_source_and_follows_as_ndjson`. `--json` list and id arguments: tasks/control.md.
- [x] `lazycloud container list [--limit 100 (max 1000)]`, `container attach ID` (streams until exit), `container checkpoint ID [--checkpoint-id]`, `container stop IDS...` (CLI/resources.py:609-712)
  Intentional: `test_container_commands_list_stop_and_attach_until_it_stops`, `test_container_attach_follows_a_pod_command_to_its_exit_code`, `test_container_checkpoint_snapshots_the_container`. Columns, stop reason, snapshot id: tasks/control.md, tasks/workloads.md.
- [x] `--json` output is one JSON document on stdout; errors as `{"error":{type,message,title,hint}}` with nonzero exit (CLI/components/errors.py:43)
  Delivered: cli/components/errors.py; `test_public_entrypoint_formats_usage_errors_as_json`, `test_volume_delete_without_tty_reports_clean_json_error`.
- [x] Task callbacks via `callback_url`: POST JSON on retry or terminal state, headers X-Task-ID/Status/Attempt/Signature/Timestamp and Idempotency-Key, 3 attempts (R/packages/execution/src/execution/callbacks.py:130)
  Intentional: internal/callbacks; `TestCallbackIsSignedAndRetriedUntilDelivered`, `TestCallbackGivesUpOnRejectionAndAfterThreeAttempts`, `TestCallbacksNeverReachPrivateAddresses`. Durable callbacks, API bodies: tasks/workload-runtime.md.
- [ ] Dashboard task drawer tabs: Result (rendered/text, download Python object, error), Logs (filter, latest 1,000 lines, download), Trace (call graph), Lifecycle timeline, Container, Artifacts; Rerun; pending notice; stop cause (WEB/components/shared/TaskDrawer/*)
  Gap: TaskDrawer/* has every tab, Rerun, pending notice and stop cause (ContainerTab.test.tsx, LogViewer tests). A pickled result offers only "Download Python object": the runner sends no text or rendered display. About 1 day (runner, protocol, storage, `Payload.display`).
- [x] Workload performance: p50/p95 latency, cold starts (WEB/routes/w/$workspace/apps/-workloads/LatencyPanel.tsx, Q/stubs.ts `taskLatencyQueryOptions`)
  Delivered: LatencyPanel.tsx over performance.sql, which counts tasks and HTTP requests; `TestWorkloadPerformanceBucketsLatencyAndColdStarts`, `TestWorkloadPerformanceCountsEndpointRequests`.
- [x] Container metrics charts (CPU/memory/GPU timeseries) (WEB/components/shared/ContainerMetricsCharts/index.tsx, Q/containers.ts)
  Intentional: ContainerMetricsCharts/*; `TestMetricSamplesComeOnlyFromTheAssignedHost`, test_observability.py. Downsampling and no disk chart: tasks/observability.md. GPU sampling unverified.
- [x] Account metrics drawer: containers, concurrency vs plan limits, tasks and failures over 24h, activity by app/resource/time range (WEB/components/shared/AppShell/AccountMetrics/*)
  Delivered: AccountMetrics/*; `TestAccountMetricsAndActivity` (CPU, GPU and container activity).
- [ ] Live updates over the SSE change stream `/api/v1/events/changes/stream`, plus container event summaries (WEB/components/shared/WorkspaceLiveUpdates/index.tsx, Q/events.ts)
  Gap: change hub, WorkspaceLiveUpdates; `TestChangeStreamDeliversCommittedChangesOfItsWorkspace`, `TestChangeStreamOverHTTP`. Volumes and usage have no topic, so those lists do not refresh live. About 1-2 h (a trigger migration).

## Notifications
- [x] Transactional email outbox (Resend): 8 attempts with backoff, bodies purged after 2 days, delivery reports via `/webhooks/resend` (R/packages/notifications/src/notifications/outbox.py, R/apps/api/src/api/server/routers/webhooks.py:127)
  Delivered: internal/notifications; `TestDeliveryRetriesAndFailures`, `TestBackoffIsCapped`, `TestResendWebhook`.
- [x] Email types: workspace invitation (R/packages/identity/src/identity/invitations.py:435) and the unfunded-storage warning (R/packages/storage/src/storage/unfunded_retention.py:102)
  Delivered: `TestInvitations`, `TestUnfundedAccountsKeepTheirDataThirtyDaysAndAreWarned`.
- [x] Task webhooks (`callback_url`) are the user-configurable notification channel (see Logs, events and metrics)
  Intentional: internal/callbacks, as in the callbacks item.
- [x] Dashboard toasts on actions: invitation sent/resent, pod scaled, deletion requested (WEB/routes/__root.tsx Toaster)
  Delivered: Toaster in __root.tsx and the same toast calls; code reading only.

## Dashboard
- [x] `/dashboard` opens the last-used or first workspace; `/w/$workspace` goes to Apps (WEB/routes/dashboard.tsx, WEB/routes/w/$workspace/index.tsx)
  Delivered: dashboard.tsx identical; smoke e2e "dashboard entry lands on Apps and the responsive shell switches workspaces".
- [ ] App shell: nav Apps/Tasks/Storage/Usage, global search (⌘/Ctrl-K or "/"), account menu (Settings, Sign out), Settings addressed as `?settings=billing|tokens|compute|domains|admin` (WEB/components/shared/AppShell/index.tsx, GlobalSearch.tsx, WEB/components/shared/SettingsDialog/view.ts)
  Gap: AppShell identical, GlobalSearch.test.tsx; global search no longer finds sandboxes (`listSandboxes` has no search). About 2 h.
- [x] Apps list: cards with 24h activity sparkline and latest workload, actions (pause/resume/delete), quickstart empty state (WEB/routes/w/$workspace/apps/index.tsx, -components/QuickstartEmptyState.tsx)
  Delivered: apps/index.tsx; apps.test.ts, onboarding e2e, stack e2e "a deployed app is listed".
- [x] App detail: header Pause/Resume/Delete, activity chart, workloads table (type filter, status/version/containers/deployed, delete workload), recent tasks, sandboxes (WEB/routes/w/$workspace/apps/$appId.tsx, -components/*)
  Delivered: $app.tsx, AppLifecycleActions.tsx, AppWorkloadsSection.tsx; stack e2e. Pause/resume untested on this page.
- [x] Workload page `/apps/$appId/workloads/$kind/$name`: Invoke URL, route/methods/ports/command, Call methods (curl, Python requests, SDK, local, typed package export), Invoke playground (JSON payload, Open task), Instances, Versions (pause/resume/delete per version), Configuration, Activity/Performance (WEB/routes/w/$workspace/apps/$appId_.workloads.$kind.$name.tsx, -workloads/*)
  Intentional: same tabs; playground-form.test.ts, stack e2e playground. Whole-workload delete, no ASGI playground, latest-URL snippets: tasks/web.md.
- [x] Nested routes: task drawer `/apps/$appId/tasks/$taskId`, workload task drawer, pod instance drawer `/instances/$containerId` (WEB/routes/w/$workspace/apps/*.tsx)
  Delivered: task, workload task and pod instance drawer routes; stack e2e task drawer.
- [x] Tasks page: infinite list with filters App/Status/Type/Workload and Clear; `/tasks/$taskId` drawer (WEB/routes/w/$workspace/tasks.tsx, tasks.$taskId.tsx)
  Intentional: tasks.tsx; tasks.test.ts. No Type filter, Workload filter after an App: tasks/web.md.
- [x] Storage page tabs: Volumes, Artifacts, Disks, Secrets, Queues, Maps (WEB/routes/w/$workspace/storage/index.tsx)
  Delivered: storage/index.tsx; stack e2e covers every tab but Disks.
- [x] Admin settings (platform admins): Users (search, role/status filters, set role, disable/enable, grant/revoke complimentary) and Fleet (markets, nodes, capacity, warm/reserve) (WEB/components/shared/SettingsDialog/AdminSettings/UsersSettings.tsx, FleetSettings.tsx)
  Intentional: AdminSettings/*; controller.test.ts, `TestAccountAdministrationOverHTTP`, `TestFleetIsForAdministratorsOnly`. Fleet without ASGs: tasks/compute.md.
- [x] Root error page with "Reload dashboard" / "Try again" (WEB/routes/__root.tsx)
  Delivered: __root.tsx identical; code reading only.
- [x] Marketing home, `/pricing`, `/legal/terms`, `/legal/privacy` (WEB/routes/index.lazy.tsx, pricing.lazy.tsx, legal/*)
  Delivered: identical routes; marketing e2e "dashboard entry remains protected while marketing routes stay public".

## CLI misc (serve, scaffolding, export, etc.)
- [x] `lazycloud serve HANDLER [--timeout 0] [--sync-dir/--sync DIR]` for App/Function/Endpoint/ASGI; rejects `--json` (CLI/serve.py:15)
  Delivered: cli/serve.py identical; `TestServePreviewSyncsSourceAndStops`, live serve. The lease is renewed while serve waits, so a 75 s start became ready (live).
- [x] Serve output: "Preview URL" header, a curl snippet (Bearer header when authorized), "Container output" header, then container logs, "Synced N files" and "Synced X changed, Y removed" on edits, "Stopping serve container" on Ctrl-C, reconnect warnings (SDK/abstractions/serve.py:180-280,395,505,722)
  Delivered: abstractions/serve.py prints the header, curl, sync and stop lines (live run in tasks/endpoints.md). A handler that fails to import stops the preview and serve prints why (live: `load_error` in 1.1 s).
- [x] Serve records a local preview that later `.remote()`, `run` and `.request()` calls from the same machine use (SDK/abstractions/serve.py:633-720)
  Delivered: `write_serve_preview`/`read_serve_preview`; `TestFunctionPreviewTakesTasksAndLapsesWithoutAFollower`, live run on the preview release.
- [x] `lazycloud example list` and `example download NAME|all [-o/--output] [--force]`: quickstart, yolo-training, openai-compatible-llm, document-processing, sandboxed-coding-agent, parallel-parquet, artifacts, all-workloads (CLI/examples.py, SDK/_examples/)
  Delivered: cli/examples.py identical, 8 examples; tests/examples (26 tests).
- [x] `lazycloud update [--check]`: self-upgrade that detects uv tool, project or pip, then verifies the new version (CLI/update.py:22, SDK/self_update.py)
  Delivered: cli/update.py, self_update.py identical; code reading only.
- [ ] `lazycloud app export` typed client codegen with `remote()` for functions and `request()` for endpoints/ASGI via OpenAPI (CLI/apps.py:24, SDK/client_codegen.py)
  Gap: functions only (test_app_export.py); no endpoint `request()` or ASGI OpenAPI methods. About 1-1.5 days.
- [ ] `lazycloud.env` helpers `is_local`, `is_remote`, `local_entrypoint`, `env_value`, and `SdkEnvVar` (SDK/env.py)
  Gap: env.py identical and `is_local`/`is_remote` work in containers, but containers get no `WORKSPACE_NAME`/`WORKSPACE_ID`, so `env_value(SdkEnvVar.WorkspaceName)` returns its default; `GatewayToken`/`GatewayHttpUrl` are never set by design. Under 1 h in internal/agent once #442 lands.
- [x] `lazycloud.schema` fields (String, Integer, Number, Boolean, JSON, File, Image, Object, Schema) for `inputs=`/`outputs=` (SDK/schema.py)
  Delivered: schema.py identical; runner applies `inputs`/`outputs`; test_python_function_contracts.py.
- [x] Destructive commands prompt for confirmation, skipped with `-y` (CLI/components/prompts.py)
  Delivered: prompts.py identical; test_public_cli.py, test_sdk_workspace_cli.py, live `-y` deletes.
- [x] Docs for agents: `docs.lazycloud.dev/llms-full.txt`, `<page>.md`, `/mcp` search server (DOCS/index.mdx)
  Delivered: docs/index.mdx and docs.json identical (hosted Mintlify features; live site not checked).
