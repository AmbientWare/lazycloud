# Parity checklist

Every user-visible capability of the reference at 9e259ce75. The rewrite
matches each one (update.md, "Same product, better implementation"). Check an
item when the rewrite provides it and its area's acceptance covers it; record
intentional differences in the area's task file. `[-]` marks an item the user
decided not to rebuild.

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
- [ ] Dashboard sign-in, GitHub only; first sign-in creates the account and an owned workspace; browser session lasts 12h (DOCS/platform/auth.mdx)
- [ ] `/signin?error=` page with a closed set of error messages (access_denied, invalid_state, account_disabled, …) (WEB/routes/signin.tsx)
- [ ] `/callback` redeems the GitHub code from the URL fragment, clears it from history, redirects to return_to or /dashboard (WEB/routes/callback.tsx)
- [ ] Sign out ends only the current browser session (Q/auth.ts `signOut`, WEB/components/shared/AppShell/AccountRail.tsx)
- [ ] `lazycloud login` device-code flow: card shows profile, endpoint, verification link and code; polls until approved, denied or expired (CLI/identity.py:107,191)
- [ ] `login` flags: `--profile`, `--endpoint`, `--workspace`, `--token`, `--tls/--no-tls`, `--activate/--no-activate` (CLI/identity.py:191)
- [ ] `/activate` page: enter code XXXX-XXXX, Approve or Deny CLI sign-in, "CLI connected" / "Sign-in denied" states (WEB/routes/activate.tsx, Q/auth.ts approve/deny)
- [ ] `lazycloud profile list|current|show [--profile]|set [--profile --endpoint --workspace --token --tls/--no-tls --activate/--no-activate]|activate NAME|delete NAME` (CLI/identity.py:273-400)
- [ ] `lazycloud token set VALUE [--profile]`, `token show [--profile]` (stored copy only) (CLI/identity.py:402,425)
- [ ] Environment variables `LAZYCLOUD_TOKEN`, `LAZYCLOUD_WORKSPACE`, `LAZYCLOUD_PROFILE`, `LAZYCLOUD_HOME` (default ~/.lazycloud/config.yaml), `LAZYCLOUD_ENDPOINT` (DOCS/cli/auth.mdx, DOCS/platform/auth.mdx)
- [ ] Access tokens in the dashboard (Settings → Tokens): create with name and expiry (1–90 days or never), value shown once, list with last used/expires/status, delete, "Show device tokens" toggle (WEB/components/shared/SettingsDialog/AccessTokens/index.tsx, Q/tokens.ts)
- [ ] Out-of-date client notice on every command, pointing to `lazycloud update` (CLI/main.py:180)

## Workspaces, members and invitations
- [ ] `lazycloud workspace list` (CLI/workspaces.py:72)
- [ ] `lazycloud workspace create NAME [--cloud aws]`: admin only, selects the new workspace, location fixed at creation (CLI/workspaces.py:87)
- [ ] `lazycloud workspace use NAME` (CLI/workspaces.py:127)
- [ ] `lazycloud workspace rename NAME` renames the current workspace (CLI/workspaces.py:159)
- [ ] `lazycloud workspace delete NAME [-y/--yes]`: admin only, asks for confirmation (CLI/workspaces.py:183)
- [ ] Dashboard workspace switcher: switch (keeps current section), Create workspace (name + location LazyCloud or AWS), Rename, Members, Delete (WEB/components/shared/AppShell/WorkspaceSwitcher.tsx, CreateWorkspaceDialog.tsx, WEB/components/shared/WorkspaceRename/Dialog.tsx)
- [ ] Workspace deletion dialog (type the name, "Delete permanently" / "Keep workspace") and a "Deletion is incomplete → Resume deletion" recovery screen (WEB/components/shared/WorkspaceDeletion/index.tsx, WEB/routes/w/$workspace/route.tsx)
- [ ] "Workspace not found" page listing accessible workspaces (WEB/routes/w/$workspace/route.tsx)
- [ ] Members dialog: list members, invite by email with role (Administrator/Member), change role, remove member or leave, resend or revoke invitation (WEB/components/shared/AppShell/WorkspaceMembersDialog.tsx, Q/members.ts)
- [ ] Invitation email: "You have been invited to <ws>", Accept button, expiry date, warning that the link joins whichever account opens it (R/packages/identity/src/identity/invitations.py:420)
- [ ] `/invitations/$token` page: preview, accept, decline, expired state (WEB/routes/invitations.$token.tsx)
- [ ] Member limits by plan: Team allows 3, Business unlimited (DOCS/platform/plans.mdx)

## Apps and deployments
- [ ] `App(slug)`: lowercase letters, digits and underscores, starts with a letter, max 63 chars (SDK/abstractions/app.py:97)
- [ ] `App.deploy(prune=, resource=, workspace=, external_url=, source_root=)`: up to 4 resources deploy concurrently (SDK/abstractions/app.py:1025)
- [ ] `App.plan(prune=, workspace=)`, `App.deployment_manifest()`, `App.combine(apps)`, `App.resources` (SDK/abstractions/app.py:110-150)
- [ ] `App.serve(resource=, timeout=, sync_dir=)` (SDK/abstractions/app.py:1126)
- [ ] `lazycloud deploy HANDLER...`: module, module:object or file.py:object; several refs must all be complete apps (CLI/execution.py:65)
- [ ] `deploy --prune/-p` (complete apps only), `--diff/-d` (table App/Kind/Workload/Action/Existing versions with add/redeploy/retain/remove), `--workspace`, `--source-root` (CLI/execution.py:65-220)
- [ ] Deploy result card: "App deployed" (app, workloads, urls, devboxes, removed_versions) or "Deployment created" (name, version, url, role, keep_warm, preemptible) (CLI/execution.py:454)
- [ ] Handler references: `module:object`, dotted package paths, `file.py:object`, bare module when it has exactly one app (DOCS/cli/overview.mdx)
- [ ] `lazycloud app list [--active|--inactive|--all]`, `app show APP`, `app pause APP`, `app resume APP`, `app delete APP`, by name or ID (CLI/apps.py:79-195)
- [ ] `lazycloud app export APP [-o/--output DIR] [--openapi res=file.json]... [--openapi-path res=/path]...`: typed package in `lazycloud_clients/<app>` (CLI/apps.py:24, SDK/client_codegen.py)
- [ ] `lazycloud deployment list [--app] [--limit 100]` (CLI/execution.py:352)
- [ ] `lazycloud deployment stop IDS_OR_NAMES...`, `start ID`, `scale ID --containers N` (pods only), `delete ID` (CLI/execution.py:377-450)
- [ ] `Deployment` handle: `id`, `name`, `stub_id`, `invoke_url(port=, url_type=)`, `submit()`, `subscribe()` (SDK/session/deployment.py:181)
- [ ] Each deploy records a new version; source is uploaded, not baked into the image; first upload writes `.lazycloudignore` (always excluded: .git .venv __pycache__ *.pyc .env .lazycloud/) (DOCS/concepts/workflow.mdx, SDK/source_sync.py:69)
- [ ] Where a call goes: a local preview first, then a fresh working-tree run from a laptop, then the deployed function when inside a container (DOCS/concepts/workflow.mdx)
- [ ] Volumes, secrets and stored data survive app deletion and pruning (DOCS/concepts/apps.mdx)

## Functions and tasks
- [ ] `@app.function(...)` options: image, name, cpu, memory, disk, gpu, gpu_count, timeout_seconds, concurrency, in_process, cron, keep_warm, max_pending_tasks, autoscaler, retries(3), retry_policy, retry_delay_seconds, callback_url, authorized, env, secrets, volumes, on_start/on_running/on_success/on_error/on_retry/on_failure/on_finish, task_policy, inputs, outputs, docker_enabled, preemptible, region, availability_zone, machine, metadata (SDK/abstractions/app.py:245)
- [ ] `Function.local()`, plain call, `.remote()`, `.async_remote()` (SDK/abstractions/function.py:243,488,564)
- [ ] `.spawn()`, `.async_spawn()` return `FunctionCall`; `.spawn_map(inputs)` submits up to 8 at a time (SDK/abstractions/function.py:517,525,574)
- [ ] `.map(inputs)` yields results in input order and `None` for a failed item (SDK/abstractions/function.py:578)
- [ ] `Function.deploy()`, `.serve()`, `.shell()`, `.prepare()`, `.spec()` (SDK/abstractions/function.py:333-487)
- [ ] `FunctionCall`: `task_id`, `task`, `result(wait=, timeout_seconds=)` giving TaskResult(ok, status, value, error, exit_code), `get()`, `logs()`, `output()`, `subscribe()`, `cancel()`, `rerun()`, `gather()` (SDK/session/task.py:250-355)
- [ ] A pending FunctionCall passed as an argument arrives as its result (dependency tracking) (SDK/abstractions/function.py:786)
- [ ] `Task.from_id(id, workspace=)`, `.get()`, `.view()`, `.pending_progress`, `.result()`, `.wait()`, `.async_wait()`, `.logs()`, `.output()`, `.subscribe()`, `.cancel()` (SDK/session/task.py:138)
- [ ] `RetryPolicy(max_attempts, delay_seconds, backoff=RetryBackoff.Fixed|Exponential, max_delay_seconds, retry_on_statuses)`, `TaskPolicy(timeout_seconds)` (SH/tasks.py:30-70)
- [ ] `current_task_id()`, `current_root_task_id()` (SDK/__init__.py)
- [ ] Arguments and results use cloudpickle from the SDK and JSON over HTTP, `--json` and exported clients; 16 MiB cap each (DOCS/concepts/functions.mdx)
- [ ] Task lifecycle: pending, running, retry, then complete/failed/timeout/cancelled/expired (DOCS/concepts/tasks-and-logs.mdx)
- [ ] Pending reasons after 5s: queued, dependencies, retry, capacity_busy, capacity_unavailable, capacity_limit, provisioning_compute, starting_container (SDK/terminal.py `_pending_card`)
- [ ] `lazycloud.progress(callback)`, `TaskPendingProgress`, `TaskPendingReason`, `PendingProgressCallback` (SDK/progress.py)
- [ ] `lazycloud run HANDLER [ARGS]... [--workspace] [--output FILE.(png|html|txt|json|pkl)]`: JSON-parses args; pod handler runs an instance; plain callable runs locally (CLI/execution.py:223)
- [ ] `lazycloud task list [--limit 100] [--app]`, `task show ID`, `task result ID [--wait/--no-wait] [--timeout] [--output]`, `task logs ID [--limit 250]`, `task stop IDS...`, `task cancel ID` (CLI/resources.py:441-606)
- [ ] Terminal steps for `.remote()`/`run`: one row per step with spinner, then ✓ or ✗, name padded to 11 chars, summary, elapsed shown as 1.2s/12s/1m 05s/1h 02m (SDK/terminal.py:53,386)
- [ ] Step "Image": "preparing", then "python 3.12 · cached" or "python X · built"; build logs indented 4 spaces, last 3 lines live, last 20 kept on failure (SDK/abstractions/image.py:636,886)
- [ ] Step "Source": "collecting files", then "syncing N files, X MB", then "syncing NN% · …", done as "N files, X MB" (SDK/source_sync.py:145-171)
- [ ] Step "Runtime": `<name>`, done as `<name> · <stub8>` (SDK/session/deployment.py:364)
- [ ] Step "Task": "submitting", then `<task8> submitted`, then `<task8> <status>` (SDK/abstractions/function.py:690,736)
- [ ] Remote stdout/stderr stream with a `│ ` rail prefix (stderr in the error color), lines flushed on newline, CR treated as newline (SDK/terminal.py:250,318)
- [ ] Pending card: "Task <id8> · pending <elapsed>", then the message, then "Next step  <hint>"; warning tone for capacity_unavailable/limit (SDK/terminal.py:330)
- [ ] Non-TTY, dumb terminal or `--json`: no live redraw, each log line printed as it arrives; all progress on stderr (SDK/terminal.py:363)
- [ ] Result on stdout: str printed raw, other values Rich-pretty-printed; "Saved <path>" with `--output`; rich display hint for images/HTML (CLI/components/results.py:20)
- [ ] `.map()`/`spawn_map()` show only the Image/Source/Runtime prepare steps once, with no per-item Task step; failures print "Task failed during map: …" (SDK/abstractions/function.py:578)
- [ ] `output(enabled=False)` silences display; calls inside containers are quiet unless wrapped in `with output():` (SDK/terminal.py:28)
- [ ] Ctrl-C or a lost connection during `.remote()` cancels the task (SDK/abstractions/function.py:706)

## Maps and queues
- [ ] `Queue(name, workspace=)`: `put`, `pop` (None when empty), `peek`, `empty`, `len()`, `delete`; values JSON-serializable (SDK/abstractions/queue.py:37)
- [ ] `Map(name, workspace=)` as a MutableMapping: `[]`, `get`, `set(key, value, ttl=)` (max 7 days, 0 means no expiry), `in`, `keys`, `len`, `del`, `delete()` (SDK/abstractions/map.py:54)
- [ ] Dashboard Storage → Queues: size, head message, Add message (JSON), Remove next message (consumes), delete queue (WEB/routes/w/$workspace/storage/-components/CollectionInspectors.tsx)
- [ ] Dashboard Storage → Maps: keys filtered by prefix, view value (binary Python values view/delete only), Edit value (JSON, expiry options, conflict check), Add key, Delete key, delete map, stats (stored/expiring/next expiry/writes) (WEB/routes/w/$workspace/storage/-components/CollectionInspectors.tsx, CollectionValueForm.tsx)

## Schedules
- [ ] `cron=` on `@app.function`, always UTC: 5-field cron, `@hourly/@daily/@midnight/@weekly/@monthly/@yearly/@annually`, `every 5m` (max 59m/23h/31d); deploy rejects invalid values (DOCS/concepts/schedules.mdx)
- [ ] Scheduled functions default to keep_warm=0; overlapping runs queue unless concurrency/autoscaler allow parallel runs (DOCS/concepts/schedules.mdx)
- [ ] Redeploying with a changed cron replaces the schedule; removing cron keeps the function callable (DOCS/concepts/schedules.mdx)
- [ ] Dashboard workload page shows Schedule, Timezone, Last run, Next run (WEB/routes/w/$workspace/apps/-workloads/WorkloadOperation.tsx, Q/cron.ts)

## Endpoints, ASGI and realtime
- [ ] `@app.endpoint(...)`: route "/", methods GET+POST, domain, workers, concurrency, keep_warm 180, max_pending_tasks 100, timeout 180, retries 0, checkpoint_enabled, authorized True, plus function options (SDK/abstractions/app.py:454)
- [ ] JSON body maps to function args; a returned Pydantic model is sent as JSON and becomes the response schema (DOCS/concepts/endpoints.mdx)
- [ ] `Endpoint.request(*args)` returns EndpointResponse(status_code, text, json()); `.target("auto"|"deployed"|"served", deployment_name=, deployment_version=)` (SDK/abstractions/endpoint.py:407,419,187)
- [ ] `Endpoint.deploy()`, `.serve()`, `.shell()`, `.local()` (SDK/abstractions/endpoint.py:384-460)
- [ ] `app.asgi(name=, route=, domain=, workers=, concurrent_requests=, keep_warm_seconds=, max_pending_tasks=, authorized=, checkpoint_enabled=, …)(fastapi_app)` (SDK/abstractions/app.py:568)
- [ ] `ASGI.request(method=, path=, json=, data=, headers=, params=, target=, deployment_name=, deployment_version=)` (SDK/abstractions/endpoint.py:780)
- [ ] `@app.realtime(name=, …)`: WebSocket handler, one call per message, an iterable return sends several messages (SDK/abstractions/app.py:663, SDK/abstractions/endpoint.py:841)
- [ ] Deployed URL `https://<name-stem>-<8hex digest>.<base>/<route>`; `-latest` means latest, `-vN` pins a version (SH/deployment_subdomains.py, SH/urls.py `build_deployment_url`)
- [ ] Stub URL `https://<stub_id>.<base>` and container URL `https://<container_id>.<base>` (SH/urls.py `build_stub_url`, `build_container_url`)
- [ ] Host routing rewrites to `/api/v1/{functions|endpoints|asgi}/{public/<id>|id/<id>|<name>/latest|<name>/vN}`; unknown hosts fall through (R/apps/api/src/api/server/host_routing.py)
- [ ] `Authorization: Bearer <token>` required unless `authorized=False`; 429 past max_pending_tasks (DOCS/concepts/endpoints.mdx)
- [ ] Functions are also HTTP-invokable: POST `/api/v1/functions/{name}/latest`, `/v{N}`, `/id/{stub}`, `/public/{stub}`, `/invoke/stream` NDJSON (R/apps/api/src/api/server/routers/functions.py)
- [ ] WebSocket routes for ASGI/realtime by id/public/latest/version (R/apps/api/src/api/server/routers/endpoints.py:333-440)

## Pods and devboxes
- [ ] `app.pod(name, image, command, ports={"http":8080}, env, cpu 1.0, memory 128Mi, disk, gpu, keep_warm 600 (-1 = always on), secrets, volumes, disks, authorized False, checkpoint_*, health_check_path/port, tcp, ssh, block_network, allow_list, docker_enabled, preemptible, region, availability_zone, machine, metadata)` (SDK/abstractions/app.py:755)
- [ ] `Pod.create(command=, timeout_seconds=)` returns PodInstance(url, terminate()); `Pod.run(*cmd)` (SDK/abstractions/pod.py:375,395,113)
- [ ] `Pod.deploy()`, `.pause()`, `.resume()`, `.scale(containers)`, `.delete()` (optional version), `.shell()` (SDK/abstractions/pod.py:407-520)
- [ ] `Container.attach(container_id=, sync_dir=, hide_logs=)` (SDK/abstractions/pod.py:133)
- [ ] Pod URL `https://<stub_id>-<port>.<base>` or `<container_id>-<port>.<base>` (SH/urls.py `build_pod_url`, `pod_proxy_url`)
- [ ] TCP pod `tls://<stub>-<port>.<tcp host>` with SNI; always public (SH/urls.py:12, DOCS/concepts/pods.mdx)
- [ ] `app.devbox(name, image, disk, cpu, memory, agent_harnesses=all, gpu, keep_warm (30min default), preemptible False, command, ports, env, secrets, volumes, disks, docker_enabled, region, machine)` (SDK/abstractions/app.py:860)
- [ ] `AgentHarness` Codex/Claude/OpenCode/Pi installed at build together with Node 22 and git (SDK/agent_harness.py, DOCS/concepts/dev-machines.mdx)
- [ ] `lazycloud devbox list [--app] [--workspace] [--limit] [--cursor]` prints "Next page: --cursor …" (CLI/devbox.py:62)
- [ ] `lazycloud devbox NAME status` card: phase, cpu, memory, disk, connections, phase reason (CLI/devbox.py:132)
- [ ] `lazycloud devbox NAME ssh [--app] [-- ssh args]` (CLI/devbox.py:115)
- [ ] `lazycloud devbox NAME login codex|claude|opencode|pi [--app]`: browser login with SSH callback forwarding, no `--json` (CLI/devbox.py:91, SDK/session/agent_login.py)
- [ ] Devbox wake phases in the connect spinner: waking up, waiting for a machine, pulling image, restoring disk, starting, connecting, saving disk (CLI/ssh.py:97)
- [ ] Dashboard pod page: Instances list (state/uptime, All/Active filter), replica stepper + Apply, instance drawer with compute, placement and terminal access (WEB/routes/w/$workspace/apps/-workloads/PodInstances.tsx, PodInstanceDrawer.tsx)
- [ ] Dashboard devbox page: Status, Connections, Disk, Idle stop, SSH config host, Start/Stop, Start logs, Files browser, Metrics, Versions (WEB/routes/w/$workspace/apps/-workloads/DevboxDetail.tsx, Q/deployments.ts)

## Sandboxes
- [ ] `app.sandbox(cpu 1.0, memory 128 (MiB), disk, gpu, gpu_count, image, keep_warm_seconds 600, authorized, name, volumes, secrets, env, sync_local_dir, block_network, allow_list, docker_enabled, preemptible, ports, region, availability_zone, machine, metadata, command)` (SDK/abstractions/app.py:949)
- [ ] `Sandbox.create()`, `.create_from_memory_snapshot(id)`, `.connect(sandbox_id)`, `.list(app_id=, limit=50)`, `.stats()`, `.timeline(stub_id, container_id=)` (SDK/abstractions/sandbox.py:1756-1840)
- [ ] `SandboxInstance`: `id`/`sandbox_id()`, `run(cmd, timeout_seconds=, cwd="/workspace", env=)`, `expose_port`, `list_urls`, `network_permissions`, `update_network_permissions(block_network=, allow_list=)`, `update_ttl`, `snapshot_memory`, `create_image_from_filesystem`, `list_processes`, `terminate` (SDK/abstractions/sandbox.py:1374)
- [ ] `instance.process`: `run`, `run_code(code, blocking=)`, `exec(*argv)`, `list_processes`; SandboxProcess `pid`, `status()`, `kill()`, `wait()`, `result()`, and `stdout`/`stderr`/`logs` streams with `read()`/`lines()` (SDK/abstractions/sandbox.py:416,549)
- [ ] `instance.fs`: `upload_file`, `download_file`, `stat_file`, `list_files`, `create_directory`, `delete_directory`, `delete_file`, `find_in_files`, `replace_in_files` (SDK/abstractions/sandbox.py:686)
- [ ] `instance.docker`: run, build, pull, push, tag, ps, images, logs, stop, rm, rmi, exec, login, compose_up/down/logs/ps/build, volume_create/ls/rm (SDK/abstractions/sandbox.py:925)
- [ ] Async `.aio` twins for instance, process, fs and docker (SDK/abstractions/sandbox.py:1522)
- [ ] Error types: SandboxConnectionError, SandboxProcessError, SandboxProcessTimeoutError, SandboxFileSystemError; data types SandboxFileInfo, SandboxFileSearch* (SDK/__init__.py)
- [ ] Dashboard app page Sandboxes section (WEB/routes/w/$workspace/apps/-components/AppSandboxesSection.tsx)
- [ ] Sandbox page: Terminal, Files (upload/download/delete), Processes (PID/command, Stop process), Network (ports/URLs), Lifecycle, Lineage, Save image, Snapshot memory (WEB/routes/w/$workspace/sandboxes/$containerId.tsx, Q/sandboxes.ts)

## Shells and SSH
- [ ] `lazycloud shell HANDLER [--sync-dir/--sync DIR] [--workspace]` or `shell --container-id ID`; rejects `--json`; "Connecting <target>" step (CLI/execution.py:265, CLI/components/progress.py:66)
- [ ] `lazycloud dev [HANDLER] [--sync ./] [--workspace]`: default dev pod uses the managed image (CLI/development.py:20)
- [ ] `lazycloud ssh NAME [--app] [--workspace] [-- ssh args]` for a devbox or an `ssh=True` pod (CLI/ssh.py:38)
- [ ] `lazycloud ssh-config [NAMES...] [--prune/-p] [--app] [--workspace]` writes ~/.lazycloud/ssh/config with hosts `lazycloud-<ws>-<app>-<name>` and includes it from ~/.ssh/config (CLI/ssh.py:157)
- [ ] Hidden `ssh-proxy POD --app` (stdio tunnel used as ProxyCommand) and `ssh-cert [--quiet] [--force]` (short-lived certificate) (CLI/ssh.py:64,135)
- [ ] `Shell`/`ShellSession` SDK (create_standalone, create_existing, connect) (SDK/abstractions/shell.py)
- [ ] Dashboard container shell dialog over WebSocket `/api/v1/shells/id/{stub}/{container}/ws` (WEB/components/shared/ShellDialog/index.tsx, Q/shells.ts)

## Images
- [ ] `Image(python_version="3.12", python_packages, commands, base_image, base_image_creds, env_vars, image_id, architecture=LinuxArchitecture.Amd64)`; Python 3.10–3.14 or an exact patch release (SDK/abstractions/image.py:145, SH/image_building/authoring.py:21)
- [ ] Factories `from_registry(uri, credentials=, python_version=)`, `from_dockerfile(path, context_dir=)`, `from_id(id)` (SDK/abstractions/image.py:200-216)
- [ ] Project factories `from_uv(dir, extras, groups)`, `from_poetry`, `from_pyproject`, `from_micromamba(environment.yml)`, all with base_image, creds and architecture (SDK/abstractions/image.py:220-340)
- [ ] Builders `add_commands`, `add_python_packages` (list or requirements path), `add_micromamba_packages`, `with_envs(clear=)`, `add_local_path(pattern)`, `with_secrets`, `build_with_gpu(hint)`, `with_docker` (SDK/abstractions/image.py:344-426)
- [ ] `Image.verify()`, `.exists()`, `.build()`, `.spec()`, `.get_credentials_from_env()` (SDK/abstractions/image.py:428-540)
- [ ] Private registry credential names per registry: GHCR, ECR, GCR/pkg.dev, ACR, NGC, Docker Hub (DOCS/concepts/images.mdx)
- [ ] Content-addressed build cache; code changes never trigger a rebuild (DOCS/concepts/images.mdx)

## Secrets
- [ ] `Secret(name, workspace=)`: `create(value)` (no overwrite), `update`, `set` (upsert), `get()`, `record()`, `delete()` (SDK/abstractions/secret.py:41)
- [ ] Workloads receive listed `secrets=[...]` as environment variables (DOCS/concepts/secrets.mdx)
- [ ] `lazycloud secret list`, `create NAME VALUE`, `modify NAME VALUE`, `show NAME [--reveal]`, `delete NAME` (CLI/secrets.py:25-117)
- [ ] Dashboard Storage → Secrets: create (SECRET_NAME + value), Rotate, Delete, values masked (WEB/routes/w/$workspace/storage/-components/SecretsTab.tsx)

## Volumes and disks
- [ ] `Volume(name, mount_path)`: `create`, `get_or_create`, `put`, `get`, `read_text`, `read_bytes`, `write_text`, `write_bytes`, `list`, `list_path`, `stat`, `move`, `remove`, `delete` (SDK/abstractions/volume.py:275-390)
- [ ] Volume presigned URLs and multipart upload: `presigned_url`, `create_multipart_upload`, `complete_multipart_upload`, `abort_multipart_upload`, `file_service_info` (SDK/abstractions/volume.py:394-480)
- [ ] `CloudBucket(name, mount_path, CloudBucketConfig(access_key, secret_key, region, bucket, prefix, endpoint, force_path_style, read_only))` (SDK/abstractions/volume.py:58,252)
- [ ] `lazycloud volume list`, `volume create NAME`, `volume delete NAME [-y]` (CLI/volumes.py:25-85)
- [ ] File commands `lazycloud ls|cp|rm|mv` with `lazycloud://vol/path` (scheme needed only on a download source; cp uploads globs) (CLI/volumes.py:89-215)
- [ ] `Disk(name, size="50Gi", mount_path="/")`: `Disk.list()`, `.delete()`, `.mount()`; sizes 1Gi–1Ti, grow only (SDK/abstractions/disk.py:23)
- [ ] `lazycloud disk list`, `disk delete NAME [-y]` (CLI/disks.py:19,52)
- [ ] Dashboard Storage → Volumes: create, browse, upload, download, delete path, delete volume, "Used by" links (WEB/routes/w/$workspace/storage/-components/VolumesTab.tsx)
- [ ] Dashboard Storage → Disks: size, stored, status, used by, delete (WEB/routes/w/$workspace/storage/-components/DisksTab.tsx)

## Artifacts
- [ ] `Artifact.file(path, content_type=, task_id=)`, `from_file(handle, suffix=)`, `from_pil_image(img, format=)`, `Artifact(path=, content_type=)` (SDK/abstractions/artifact.py:92-180)
- [ ] `save(target_dir=, task_id=)` returns SavedArtifact(artifact_id, task_id, filename, expires_at); directories are zipped (SDK/abstractions/artifact.py:244)
- [ ] `public_url(expires=3600)`, `exists()`, `stat()`, `delete()`, `zip_dir()`, `package()`, `save_remote()` (SDK/abstractions/artifact.py:200-300)
- [ ] Retention by plan: Free 1 day, Team 30, Business 90 (DOCS/concepts/artifacts.mdx)
- [ ] `lazycloud artifact list [--workspace] [--task-id] [--search] [--cursor]`, `artifact usage`, `artifact delete ID [-y]` (CLI/artifacts.py:25-62)
- [ ] Dashboard Storage → Artifacts: search, filters (app/type/task/saved before/from), bulk select delete, preview, download, View task, refresh, retention column; storage usage summary (WEB/components/shared/Artifacts/index.tsx, ArtifactRow.tsx, ArtifactPreview.tsx)

## Custom domains
- [ ] `lazycloud domain add HOSTNAME` prints phase, CNAME and ownership records, plus a hint to run `domain status` (CLI/domains.py:76)
- [ ] `lazycloud domain status HOSTNAME` (single read, no waiting), `domain list`, `domain remove HOSTNAME` (CLI/domains.py:103-147)
- [ ] `domain="…"` on endpoint/asgi/realtime must be the registered name or a subdomain of it; Team/Business only (DOCS/platform/domains.mdx)
- [ ] Custom hostname routing (R/apps/api/src/api/server/host_routing.py `_custom_hostname_target`)
- [ ] Dashboard Settings → Domains: add, DNS record table (Type/Name/Target/Value), status, remove, "Upgrade to Team" gate (WEB/components/shared/SettingsDialog/DomainSettings.tsx)

## Compute (managed, AWS connect, joined machines)
- [ ] `lazycloud compute status`, `compute instances`, `compute workloads` (CLI/resources.py:63-130)
- [ ] `lazycloud cloud connect aws --account-id [--role-arn] [--networks-json]` (CLI/resources.py:131)
- [ ] `lazycloud cloud authorize [--profile AWS_PROFILE]` submits a CloudFormation stack from the local machine (CLI/resources.py:207)
- [ ] `cloud validate`, `cloud status [--watch --until PHASE --interval 2 --timeout 600]`, `cloud reconnect [--role-arn]`, `cloud cancel-reconnect`, `cloud retry`, `cloud disconnect [--open/--no-open] [--wait/--no-wait] [--interval] [--timeout]` (CLI/resources.py:176-410)
- [ ] `lazycloud machine join --name --workspaces a,b [--gpu] [--max-cpu] [--max-memory] [--max-gpus|--gpu-ids] [--background/--foreground] [--service-manager] [--service-name] [--state-dir]` (CLI/resources.py:754, CLI/machine_join.py)
- [ ] `lazycloud machine list`, `machine update NAME --workspaces`, `machine remove ID` (CLI/resources.py:714-868)
- [ ] Machine phases: requested, provisioning, booting, joining, ready, draining, terminating, deleted, failed; offline badge (DOCS/platform/compute.mdx, WEB/lib/machine-lifecycle.ts)
- [ ] Placement options `machine=`, `region=` (us-east/us-west/eu-central/eu-north/ap-southeast), `availability_zone=`, `preemptible=` (DOCS/concepts/resources.mdx)
- [ ] GPU types `GpuType.T4/A10G/L4/L40S/A100_40/A100_80/H100/H200/Any`, preference lists; plain A100 rejected (SH/gpu.py, DOCS/concepts/resources.mdx)
- [ ] `Autoscaler(min_containers=0, max_containers=1, tasks_per_container=1)` (SH/autoscaling.py)
- [ ] Agent install endpoints `/install/agent` and `/install/agent/{os}/{arch}` (R/apps/api/src/api/server/routers/install.py)
- [ ] Dashboard Settings → Compute: Connected clouds (Add cloud, AWS details, instances, "Upgrade to Business" gate), Self-hosted machines list, edit machine workspaces (WEB/components/shared/SettingsDialog/ComputeSettings.tsx, MachineWorkspaces.tsx)
- [ ] AWS connection dialog: account ID, Continue to AWS, Check authorization, Reconnect, Cancel reconnect, Retry, Remove; last validated (WEB/components/shared/SettingsDialog/AwsConnectionDialog/index.tsx)
- [ ] Join machine dialog: name + workspaces, Generate install command, host requirements, live connection status (WEB/components/shared/SettingsDialog/JoinMachineDialog.tsx)

## Billing and plans
- [ ] Plans Free/Team/Business: prepaid credit balance, trial and subscription credit, per-second compute (DOCS/platform/plans.mdx)
- [ ] Placement multipliers: preemptible=False 3× CPU/memory; pinned region 1.5× (DOCS/platform/plans.mdx)
- [ ] Limits: concurrency, zero balance stops work, monthly usage limit resets on the 1st, 30-day unfunded retention (DOCS/platform/plans.mdx)
- [ ] Email: "Add credit within 30 days to keep your stored data" (R/packages/storage/src/storage/unfunded_retention.py:104)
- [ ] Settings → Billing, Plan and payment: current plan, Change plan dialog (proration, scheduled downgrade, cancel scheduled change), add/update payment method, Invoices portal, past-due state (WEB/components/shared/SettingsDialog/BillingSettings/index.tsx, PlanDialog.tsx)
- [ ] Prepaid credit: available balance, Add credits with preset or custom amount (Stripe checkout) (WEB/components/shared/SettingsDialog/BillingSettings/PrepaidCredit.tsx, AmountSelect.tsx)
- [ ] Spending controls: monthly usage limit, automatic reload (threshold + amount), resume reload after a declined payment (WEB/components/shared/SettingsDialog/BillingSettings/BillingPreferences.tsx, Q/billing.ts)
- [ ] Credit prompt in the app shell (WEB/components/shared/AppShell/CreditPrompt.tsx)
- [ ] Public `/pricing` page and pricing catalog `/api/v1/pricing` (WEB/routes/pricing.lazy.tsx, Q/pricing.ts)
- [ ] Usage page: range control, usage cost, amount covered by subscription credits, spend chart by category, breakdown by app and workload with runtime and cost, "View run", image builds, usage without an app (WEB/routes/w/$workspace/usage/index.tsx and -components/*)
- [ ] Stripe webhook `/webhooks/stripe` (R/apps/api/src/api/server/routers/webhooks.py:63)

## Logs, events and metrics
- [ ] `lazycloud logs (--deployment|--task-id|--container-id) [-n/--lines 250 (1–1000)] [--show-timestamp] [-f/--follow] [--max-events 0]`; lines optionally `[ISO-ts] msg`; `--follow --json` prints NDJSON; empty result prints "No log entries found." (CLI/logs.py:16)
- [ ] `lazycloud container list [--limit 100 (max 1000)]`, `container attach ID` (streams until exit), `container checkpoint ID [--checkpoint-id]`, `container stop IDS...` (CLI/resources.py:609-712)
- [ ] `--json` output is one JSON document on stdout; errors as `{"error":{type,message,title,hint}}` with nonzero exit (CLI/components/errors.py:43)
- [ ] Task callbacks via `callback_url`: POST JSON on retry or terminal state, headers X-Task-ID/Status/Attempt/Signature/Timestamp and Idempotency-Key, 3 attempts (R/packages/execution/src/execution/callbacks.py:130)
- [ ] Dashboard task drawer tabs: Result (rendered/text, download Python object, error), Logs (filter, latest 1,000 lines, download), Trace (call graph), Lifecycle timeline, Container, Artifacts; Rerun; pending notice; stop cause (WEB/components/shared/TaskDrawer/*)
- [ ] Workload performance: p50/p95 latency, cold starts (WEB/routes/w/$workspace/apps/-workloads/LatencyPanel.tsx, Q/stubs.ts `taskLatencyQueryOptions`)
- [ ] Container metrics charts (CPU/memory/GPU timeseries) (WEB/components/shared/ContainerMetricsCharts/index.tsx, Q/containers.ts)
- [ ] Account metrics drawer: containers, concurrency vs plan limits, tasks and failures over 24h, activity by app/resource/time range (WEB/components/shared/AppShell/AccountMetrics/*)
- [ ] Live updates over the SSE change stream `/api/v1/events/changes/stream`, plus container event summaries (WEB/components/shared/WorkspaceLiveUpdates/index.tsx, Q/events.ts)

## Notifications
- [ ] Transactional email outbox (Resend): 8 attempts with backoff, bodies purged after 2 days, delivery reports via `/webhooks/resend` (R/packages/notifications/src/notifications/outbox.py, R/apps/api/src/api/server/routers/webhooks.py:127)
- [ ] Email types: workspace invitation (R/packages/identity/src/identity/invitations.py:435) and the unfunded-storage warning (R/packages/storage/src/storage/unfunded_retention.py:102)
- [ ] Task webhooks (`callback_url`) are the user-configurable notification channel (see Logs, events and metrics)
- [ ] Dashboard toasts on actions: invitation sent/resent, pod scaled, deletion requested (WEB/routes/__root.tsx Toaster)

## Dashboard
- [ ] `/dashboard` opens the last-used or first workspace; `/w/$workspace` goes to Apps (WEB/routes/dashboard.tsx, WEB/routes/w/$workspace/index.tsx)
- [ ] App shell: nav Apps/Tasks/Storage/Usage, global search (⌘/Ctrl-K or "/"), account menu (Settings, Sign out), Settings addressed as `?settings=billing|tokens|compute|domains|admin` (WEB/components/shared/AppShell/index.tsx, GlobalSearch.tsx, WEB/components/shared/SettingsDialog/view.ts)
- [ ] Apps list: cards with 24h activity sparkline and latest workload, actions (pause/resume/delete), quickstart empty state (WEB/routes/w/$workspace/apps/index.tsx, -components/QuickstartEmptyState.tsx)
- [ ] App detail: header Pause/Resume/Delete, activity chart, workloads table (type filter, status/version/containers/deployed, delete workload), recent tasks, sandboxes (WEB/routes/w/$workspace/apps/$appId.tsx, -components/*)
- [ ] Workload page `/apps/$appId/workloads/$kind/$name`: Invoke URL, route/methods/ports/command, Call methods (curl, Python requests, SDK, local, typed package export), Invoke playground (JSON payload, Open task), Instances, Versions (pause/resume/delete per version), Configuration, Activity/Performance (WEB/routes/w/$workspace/apps/$appId_.workloads.$kind.$name.tsx, -workloads/*)
- [ ] Nested routes: task drawer `/apps/$appId/tasks/$taskId`, workload task drawer, pod instance drawer `/instances/$containerId` (WEB/routes/w/$workspace/apps/*.tsx)
- [ ] Tasks page: infinite list with filters App/Status/Type/Workload and Clear; `/tasks/$taskId` drawer (WEB/routes/w/$workspace/tasks.tsx, tasks.$taskId.tsx)
- [ ] Storage page tabs: Volumes, Artifacts, Disks, Secrets, Queues, Maps (WEB/routes/w/$workspace/storage/index.tsx)
- [-] Admin settings (platform admins): Users (search, role/status filters, set role, disable/enable, grant/revoke complimentary) and Fleet (markets, nodes, capacity, warm/reserve) (WEB/components/shared/SettingsDialog/AdminSettings/UsersSettings.tsx, FleetSettings.tsx) — dropped: unused (user decision)
- [ ] Root error page with "Reload dashboard" / "Try again" (WEB/routes/__root.tsx)
- [ ] Marketing home, `/pricing`, `/legal/terms`, `/legal/privacy` (WEB/routes/index.lazy.tsx, pricing.lazy.tsx, legal/*)

## CLI misc (serve, scaffolding, export, etc.)
- [ ] `lazycloud serve HANDLER [--timeout 0] [--sync-dir/--sync DIR]` for App/Function/Endpoint/ASGI; rejects `--json` (CLI/serve.py:15)
- [ ] Serve output: "Preview URL" header, a curl snippet (Bearer header when authorized), "Container output" header, then container logs, "Synced N files" and "Synced X changed, Y removed" on edits, "Stopping serve container" on Ctrl-C, reconnect warnings (SDK/abstractions/serve.py:180-280,395,505,722)
- [ ] Serve records a local preview that later `.remote()`, `run` and `.request()` calls from the same machine use (SDK/abstractions/serve.py:633-720)
- [ ] `lazycloud example list` and `example download NAME|all [-o/--output] [--force]`: quickstart, yolo-training, openai-compatible-llm, document-processing, sandboxed-coding-agent, parallel-parquet, artifacts, all-workloads (CLI/examples.py, SDK/_examples/)
- [ ] `lazycloud update [--check]`: self-upgrade that detects uv tool, project or pip, then verifies the new version (CLI/update.py:22, SDK/self_update.py)
- [ ] `lazycloud app export` typed client codegen with `remote()` for functions and `request()` for endpoints/ASGI via OpenAPI (CLI/apps.py:24, SDK/client_codegen.py)
- [ ] `lazycloud.env` helpers `is_local`, `is_remote`, `local_entrypoint`, `env_value`, and `SdkEnvVar` (SDK/env.py)
- [ ] `lazycloud.schema` fields (String, Integer, Number, Boolean, JSON, File, Image, Object, Schema) for `inputs=`/`outputs=` (SDK/schema.py)
- [ ] Destructive commands prompt for confirmation, skipped with `-y` (CLI/components/prompts.py)
- [ ] Docs for agents: `docs.lazycloud.dev/llms-full.txt`, `<page>.md`, `/mcp` search server (DOCS/index.mdx)
