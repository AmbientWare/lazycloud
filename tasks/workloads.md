# Workloads packet

Parity sections "Pods and devboxes", "Sandboxes" and "Shells and SSH" from
tasks/parity.md, plus the items endpoints and observability handed off:
`checkpoint_enabled` and memory snapshots, `lazycloud container checkpoint`,
endpoint shells, and the dashboard APIs those pages need. Migration
`migrations/0011_workloads.sql`, protobuf fields 90-99, OpenAPI operations
tagged `workloads`.

## Outcome

`lazycloud deploy tools:web` runs a pod whose URL answers through the edge,
wakes on the first request and stops after `keep_warm` idle seconds;
`web.scale(2)` holds two. `web.create()` starts an instance with a URL and
`terminate()`. `lazycloud ssh box --app a` wakes a pod or devbox and opens a
root shell over a short-lived certificate; `ssh-config` makes plain `ssh` and
editors work. `sandbox.create()` gives an instance whose processes, files,
ports, network policy, TTL, filesystem image and memory snapshot work as in
the reference. `lazycloud shell` and the dashboard shell open a PTY in any
container.

## Design

- One container model. Pods (kind `pod`; a devbox is a pod with
  `pod.kind = devbox`) and sandboxes (kind `sandbox`) are workloads whose
  release spec has a `pod` section; execution owns their containers like any
  other. `containers.purpose` separates the containers planning counts
  (`serve`) from those started on request (`instance`: `Pod.create`,
  `Sandbox.create`; `shell`: standalone shells). Planning never counts,
  drains or replaces an instance, and a shell container of a function's
  release claims no tasks.
- Idle. `containers.active_until` is when a container stops being active;
  readiness, API calls and `update_ttl` push it to
  `now + keep_warm_seconds`. `container_leases` rows are open connections
  (shells, SSH tunnels, TCP, HTTP through an edge), renewed every 10 s by
  their holder with a 30 s TTL; an ended lease keeps the container active for
  its window. A container is idle when `active_until` passed and no lease is
  inside its window. The scheduler drains idle instances; planning drains
  idle pod containers above the pod's count. This replaces the reference's
  Redis keep-warm locks and connection counters, which never expired when a
  proxy died.
- Pods. `PlanPods` runs under the planning lock beside `Plan` and
  `PlanServing`. Without a scale a pod runs one container while it was woken
  in the last 15 minutes or a container is still warm; `keep_warm = -1`
  keeps one always. `pod_states.replicas` (scale) holds a count. A
  connection to a cold pod wakes it and waits for the container-ready
  notification (175 s), or fails at once with the start error. A stopped
  devbox is parked until the next connection or Start.
- Host. The supervisor is PID 1 everywhere. In pod mode it runs the command
  instead of runner slots and is ready once its health check, or else its
  first port, answers. Every supervisor serves the control API below on
  `control.sock` in the link directory; the agent reaches it from the data
  connection (`RequestHead.control`, `RequestHead.port`), so processes,
  files, shells, SSH and port traffic reuse the endpoints packet's
  edge-to-agent transport and its relays. The framed shell server and HMAC
  passwords are gone: a shell is a PTY process behind a WebSocket.
- SSH. The workspace's user CA (`ssh_authorities`) and each pod's host key
  (`ssh_host_keys`) are ed25519 keys sealed with the secrets owner's master
  key (`Secrets.Seal`), created on first use, one row each, so either
  rotates by replacing its row. Certificates last 12 hours for principal
  `root`. The CLI bridges `ssh` through `GET .../apps/{app}/pods/{pod}/ssh`
  (WebSocket, opened before the pod is ready); the server upgrades the
  supervisor's `/ssh` into a byte tunnel over the data connection.
- Pod ports. `<release id>-<port>` and `<container id>-<port>` under the edge
  base answer HTTP and WebSockets through the supervisor's port tunnel; a
  release host picks a ready container and wakes a cold pod. TCP pods answer
  `tls://<id>-<port>.<tcp host>`: the edge terminates TLS (`-edge-tcp-*`
  flags), routes by SNI and tunnels the bytes.
- Network policy. The agent applies `block_network`/`allow_list` as an
  nftables netdev egress filter on the container's interfaces, from a helper
  container in the container's network namespace with NET_ADMIN, before the
  command starts; `UpdateNetwork` (versioned by `network_version`) changes
  it later. The filter reads the frame's own EtherType and destination and
  drops all but ARP and allowed ranges, so a raw socket's protocol label
  cannot slip past it. A policy refuses `docker_enabled` (nested containers
  bypass it).
- Isolation. The agent connects only to a socket inode inside the link
  directory, never through a link the workload left there, which resolves
  on the host. The directory is sticky and world-writable so any container
  user creates its sockets. Containers other than serving functions get no
  platform API; a function reaches only the instances it started, and no
  workload control. `docker_enabled` runs privileged only under runsc or
  with the agent's `-allow-privileged-docker`.
- Snapshots. `SnapshotContainer` makes the agent run `docker checkpoint
  create --leave-running`, upload the archive to
  `workspaces/<ws>/snapshots/<id>.tar` and report `CompleteSnapshot`; a host
  that cannot checkpoint reports `unsupported`. A start with `restore`
  starts from the checkpoint. `checkpoint` releases get an automatic
  snapshot of their first ready container (after the pod's readiness probe)
  and restore later cold starts, falling back to a cold start that marks the
  snapshot failed. Before a checkpoint the supervisor closes its host
  sockets (`Detach`) and reopens them once the link is back.
- Network holders. Pods, sandboxes, devboxes and functions with
  `checkpoint` start `checkpointable`: the agent starts a holder container
  (`lazycloud-<id>-net`, busybox `sleep`, no capabilities) while the image
  and source are prepared, applies the start policy in its namespace, and
  runs the container with `--network container:<holder>`. Docker cannot
  restore into a container with a network of its own (moby#50750), and
  runsc refuses a restore whose sysctls differ; a joined namespace avoids
  both, and a restored copy is filtered before it runs. The holder is
  removed with its container, adopted by label, and removed when its
  container is gone. Only checkpointable containers can be snapshotted.
  Plain functions keep their own network.
- Filesystem images. `PublishFilesystem` streams the supervisor's tar of `/`
  without mounts into `docker import`, pushes it to
  `<registry>/<repo>/filesystems/<workspace>` and reports
  `CompleteFilesystemImage`; the images owner registers it
  (`Images.RegisterFilesystem`), so `Image.from_id` runs it.
- Devboxes. The root disk is a storage disk declared at `/` and named after
  the devbox. The agent mounts it at `/lazycloud/root`; the supervisor seeds
  it from the image on first use, binds `/proc`, `/dev`, `/sys`,
  `/run/lazycloud`, `/etc/hosts`, `/etc/resolv.conf` and the other mounts
  into it, and chroots, so the command and every session see it as `/`.
  It then drops CAP_SYS_ADMIN from every thread's bounding and ambient sets
  and sets no_new_privs, so no process in the devbox can mount; AppArmor
  stays unconfined for the container's life.
  Phases come from the container state and its start stages (`image`,
  `disk`).

## Supervisor control API

HTTP/1.1 on `control.sock`. Bodies are the public schemas; errors are the
`Error` schema. The server forwards the public operation under
`/v1/workspaces/{ws}/containers/{c}` to the same path here.

| Request | Public operation |
| --- | --- |
| `GET /processes`, `POST /processes` | listProcesses, startProcess |
| `GET /processes/{id}?wait_seconds=`, `POST /processes/{id}/kill` | getProcess, killProcess |
| `GET /files?path=&limit=`, `DELETE /files?path=`, `GET /files/stat?path=` | listContainerFiles, deleteContainerFile, statContainerFile |
| `GET /files/content?path=&max_bytes=&truncate=`, `PUT /files/content?path=&mode=` | downloadContainerFile, uploadContainerFile |
| `POST /files/find`, `POST /files/replace` | findInContainerFiles, replaceInContainerFiles |
| `POST /directories?path=&mode=`, `DELETE /directories?path=` | createContainerDirectory, deleteContainerDirectory |
| `GET /shell?cols=&rows=&term=` (WebSocket) | openContainerShell |
| `GET /ssh` with `Upgrade: lazycloud-tunnel`, then SSH bytes | openSshTunnel |
| `GET /ports/{port}` with `Upgrade: lazycloud-tunnel`, then TCP bytes | pod and sandbox ports, TCP pods |
| `POST /filesystem`, an `application/x-tar` of `/` without mounts | createFilesystemImage |

Processes keep the reference's limits: 256 KiB retained per stream, a 64 MiB
budget for all output, results kept 5 minutes after exit, at most 65,536
records, a one-second output drain after exit, exit code 128+N for signal N.

## Delivered

| Parity item | Evidence |
| --- | --- |
| `app.pod(...)` options, deploy, URL, wake on request, keep_warm, `-1` always on | `TestPodsFollowConnectionsScalesAndParks`, `TestAlwaysOnPodsRefuseScalingToZero`, `TestPodDefinitionsResolveTheirDefaults`, `TestPodDefinitionsRejectWhatTheyCannotRun`, `TestPodsDeployAndPrepareUnderTheirKind`; live `test_a_pod_deploys_answers_on_its_url_scales_and_starts_instances` |
| `Pod.create/run/terminate`, `scale`, `pause/resume/delete`, `deployment scale --containers` | live pod test; SDK `test_pod_*`; `deployment scale tcp --containers 2` printed "Set tcp to 2 containers." |
| Pod URLs `<release>-<port>` and `<container>-<port>`; TCP pods over TLS with SNI | live pod test; TLS client against `tls://<release>-8080.tcp.lazycloud.localhost` answered `HTTP/1.0 200` |
| `Container.attach`, `container attach`, `container checkpoint` | SDK `test_container_attach_follows_a_pod_command_to_its_exit_code`, `test_container_checkpoint_snapshots_the_container`; agent `TestPodCommandExitReportsItsCode` |
| `app.devbox(...)`, `devbox list/status/ssh/login`, phases, Start/Stop | `TestPodsFollowConnectionsScalesAndParks` (park, wake, phases); CLI against the stack: list, status card, a failed start reported at once with its reason |
| `AgentHarness` install commands | unchanged SDK recipe, `test_app_deploy_sends_pods_and_devboxes_with_their_defaults_and_never_sandboxes` |
| `app.sandbox(...)`, `create`, `connect`, `list`, `stats`, `timeline`, `create_from_memory_snapshot` | `TestInstancesStopOnceIdleAndConnectionsKeepThemUp`, `TestSnapshotsAreReportedOnceByTheirHost`; SDK `test_sandbox_*` |
| `SandboxInstance.run`, `process.*`, `fs.*`, `expose_port`, `list_urls`, network, `update_ttl`, `terminate`, `.aio` | live `test_a_sandbox_runs_processes_and_files_and_controls_its_network`; supervisor process and file tests |
| `create_image_from_filesystem`; `snapshot_memory` | live: an image of a sandbox ran in a new sandbox with the file written before; snapshot answered `unsupported` with the CRIU message (runc, no CRIU) |
| `instance.docker` | agent `TestDockerPodRunsADaemon` (docker:28.5.1-dind) |
| `lazycloud shell`, `shell --container-id`, `dev`, `Shell`/`ShellSession`, endpoint and ASGI shells | live pod test (standalone shell ran a command and exited 7); supervisor `TestShellRunsALoginShellOnAPTY`; SDK `test_shell_waits_for_its_container_and_bridges_bytes_resizes_and_exit` |
| `lazycloud ssh`, `ssh-config`, `ssh-proxy`, `ssh-cert` | `TestSSHCertificatesAreSignedByTheWorkspaceAuthority`, supervisor `TestSSHAcceptsOnlyAuthorityCertificatesForRoot`, `TestSSHSessionsExecShellSignalSFTPAndForwarding`; `lazycloud ssh box --app sshcheck -- 'echo ...'` and plain `ssh -F` against the stack |
| `checkpoint_enabled` | automatic snapshot requests and restore selection: `TestSnapshotsAreReportedOnceByTheirHost`; agent `TestFailedRestoreStartsColdOnlyForAutomaticSnapshots` |
| Network block and allow lists | supervisor `TestNetfilterBlocksAndAllowsEgressInAContainer`, agent `TestNetworkPolicyBlocksEgressUntilAllowed`, live sandbox test |

## Measurements

One host (24 CPUs, Docker 29, runc), the private stack in /tmp/wl with
server, scheduler and agent from this branch, images cached:

| Scenario | Result |
| --- | --- |
| `sandbox.create()` to ready (prepare, admit, place, start, connect) | p50 0.26 s, max 0.36 s (5 runs) |
| `instance.run("true")` round trip (API, edge, data connection, supervisor) | p50 4.0 ms, p95 4.6 ms (50 runs) |
| 64 MiB `fs.upload_file` / `download_file` | 196 MiB/s up, 296 MiB/s down |
| `ssh -F <config> <alias> true` to a warm pod | p50 1.15 s, p95 1.38 s (two CLI process starts for `ssh-cert` and `ssh-proxy` included) |
| `lazycloud ssh box -- true` | p50 1.67 s |
| TCP pod over TLS, cold (wake) / warm | 0.34 s / 0.04 s |
| Filesystem image of a sandbox (tar, import, push, register) | 4.4 s |
| A devbox connection whose start fails | fails in 1.8 s with the start error, not at the 175 s deadline |

## Under gVisor

runsc 20260928 (`--host-uds=all`, no `--net-raw`, no `--nvproxy`), Docker 29,
an unprivileged agent. `LAZYCLOUD_TEST_OCI_RUNTIME=runsc` runs the agent and
supervisor Docker tests under it; all pass but the GPU test.

| Check | Result |
| --- | --- |
| Functions, endpoints, pods, sandboxes on the private stack | `double.remote(21)` 42, endpoint 42, live pod and sandbox suites pass, `dmesg` says gVisor |
| Supervisor sockets created inside the sandbox | link, control, HTTP and port tunnels work; the agent's link-refusing dial holds (`TestSupervisorSocketsAreNeverReachedThroughALink`) |
| Network block, allow list, update | netfilter test and live: open reaches 1.1.1.1, block refuses, `allow 1.1.1.1/32` reaches it and refuses 8.8.8.8; each update returns once the host applied it |
| Raw-socket bypass | not possible: gVisor refuses packet sockets without `--net-raw` (the test skips); under runc the EtherType filter drops it |
| Devbox root switch and capability drop | `TestDevboxRootPersistsWritesAndKeepsProc` passes: CapBnd and CapEff lack CAP_SYS_ADMIN, NoNewPrivs 1, `mount` fails |
| `docker_enabled` | runs with every capability instead of privileged, which gVisor cannot start; `docker info` 29.8.2 in a sandbox, a nested `docker run` printed its output; refused under runc without `-allow-privileged-docker` |
| Filesystem images | include gVisor's tmpfs `/tmp`; a sandbox from the image read both files |
| Memory snapshots | the checkpoint is taken after the supervisor detaches (0.18 s), and the pod serves again 0.5 s later (`TestSnapshotUnderRunscDetachesAndKeepsServing`); reading it needs a root agent, so an unprivileged one reports `unsupported`. With a root agent `snapshot_memory()` uploads in 0.2 s and the sandbox keeps answering |
| Restore from a snapshot | through Docker with a network holder (above): with the docker CLI a counting process resumed where it stopped 0.15 s after `docker start --checkpoint`, with a new environment value, a new mount source and working egress. Without one Docker fails with `bind-mount /proc/0/ns/net` (moby#50750). `TestSnapshotUnderRunscRestoresARunningPod` checks it end to end with a root agent |
| Devbox on an NBD root disk (root agent) | live `test_ssh_config_makes_plain_ssh_reach_a_devbox`: deploy, seed the root disk (137 MB stored, generation 1), plain `ssh -F`, `lazycloud devbox <name> ssh -- echo devbox-ok`, `devbox status`, delete |
| Everything above again with the root agent | `runsc_live.py` and the live suite pass |
| With network holders and a root agent (snapshot-restore) | `runsc_live.py` (functions, endpoints, sandbox ports, block, allow list, filesystem image, snapshot upload, docker) and the live suite with the devbox pass; the first restore on the snapshot's host hit moby#42900, fixed with the restore marker |
| Restore with holders and a root agent | `root_live.py`: snapshot in 0.74 s; restore to ready in 0.51 s with a counting process resuming (13 at restore, 18 a second later) rather than restarting; the same snapshot restores again on the host that took it; the restored sandbox reaches the network and a block applied after restore holds; a `block_network` sandbox stays blocked across its own snapshot and restore |

## Intentional differences from the reference

- Paths follow the new API (`/v1/workspaces/{ws}/instances`,
  `.../containers/{c}/processes|files|ports|network|ttl|snapshots|
  filesystem-images|shell`, `.../sandboxes`, `.../ssh/...`,
  `.../deployments/{d}/scale|devbox`).
- File uploads and downloads carry raw bytes, not base64 in JSON; both stop
  at 64 MiB per request, the reference's download limit.
- `find_in_files` returns every occurrence, not the first per file, up to
  10,000 matches; replace reports the files and replacements it changed.
- Sandbox listings are one row per sandbox container (`id` is what
  `Sandbox.connect` takes); the reference listed definitions.
- `keep_warm_seconds` and `update_ttl` are an idle lifetime: API calls,
  shells, tunnels and HTTP traffic keep an instance up, as the docs said. The
  reference's lock expired from creation however busy the sandbox was.
- Leases expire when the holder dies; the reference's Redis counters stayed
  up forever after a proxy crash.
- Shell sessions have no username or password; the WebSocket protocol is
  binary terminal bytes plus JSON resize/exit/error messages. The dashboard
  authenticates the upgrade with its session cookie and Origin instead of a
  single-use ticket.
- A connection to a pod whose start fails is refused with the reason at
  once.
- SSH host keys are per pod and the CA per workspace, stored sealed; the
  reference derived both from one workspace secret, so neither could rotate.
- Filesystem images leave out files the container's user cannot read.
- `Container.attach()` returns a `ContainerAttachment` (output, exit code,
  stop reason) and `Container` takes `workspace=` instead of
  `endpoint=`/`token=`.
- The SSH config's ProxyCommand and certificate command run
  `<python> -m lazycloud.cli.main ssh-proxy|ssh-cert` with the running
  interpreter, so an older `lazycloud` first on PATH cannot answer them.
- `container checkpoint --checkpoint-id` takes a snapshot id.
- `docker_enabled` cannot be combined with `block_network` or `allow_list`,
  because nested containers would bypass the policy. The reference accepted
  both.

## Gaps

- Snapshot upload and restore need a root agent, as in production; CRIU is
  not installed here, so runc checkpoints only reach `unsupported`.
- Docker uploads a checkpoint to containerd before restoring it and fails
  with `content ... already exists` when containerd holds that content
  (moby#42900), as on the host that took the snapshot and on any second
  restore there. The agent adds a `lazycloud-restore` file naming the
  restoring container, so each restore's content is new; runsc reads only
  its image files.
- Disks need a plan with a disk allowance; the private stack's account was
  made complimentary (`server admin set-complimentary`) to get one.
- GPUs under runsc need `--nvproxy` in the runtime's arguments; this host's
  runsc has none, so `TestAgentGivesContainersFreeGPUs` times out there.
- A checkpoint closes the container's open shells, tunnels and HTTP
  connections, since no runtime saves a host socket.
- Under runc `docker_enabled` needs `-allow-privileged-docker` and gives the
  workload host privilege; `LAZYCLOUD_ALLOW_PRIVILEGED_DOCKER=true` sets it
  for `deploy/local/run.sh`.
- The TCP ingress needs a wildcard certificate and a listener in the Helm
  chart (operations).
- Dashboard pages come with the web packet; their operations are below.

## API for the web packet

- Pod page: `GET .../containers?deployment=&live=` (instances: state,
  `ready_at`/`stopped_at` for uptime, `host`, `gpu_count`, `kind`,
  `purpose`, `expires_at` for an instance with a timeout),
  `POST .../deployments/{d}/scale`, `GET .../deployments/{d}`
  (`role`, `scaling`, `url`), the container drawer's
  `GET .../containers/{c}`, `GET .../containers/{c}/metrics`.
- Devbox page: `GET .../deployments/{d}/devbox` (phase, reason,
  connections, idle deadline, disk, SSH command and host, resources),
  `POST .../devbox/start`, `POST .../devbox/stop`; start logs
  `GET .../containers/{failed_container_id}/output`; files
  `GET .../containers/{c}/files`, `GET .../files/content`; metrics
  `GET .../containers/{c}/metrics`; versions
  `GET .../deployments/{d}/versions`.
- Sandbox page: terminal `GET .../containers/{c}/shell` (WebSocket); files
  list/upload (`PUT .../files/content`)/download/delete; processes
  `GET .../processes`, stop `POST .../processes/{p}/kill`; network
  `GET .../ports`, `GET .../network`; lifecycle
  `GET .../containers/{c}/lifecycle`; save image
  `POST .../filesystem-images`; snapshot memory `POST .../snapshots`; stop
  `POST .../containers/{c}/stop`.
- App page Sandboxes section: `GET .../sandboxes?app=` and
  `GET .../sandboxes/stats?app=`.
- Container shell dialog: `GET .../containers/{c}/shell?cols=&rows=&term=`.

## Try it

Run the stack (`deploy/local/run.sh start`), export its SDK environment,
then `lazycloud deploy tools:web` for a pod, `sandbox.create()` from Python,
`lazycloud shell --container-id <id>`, and for SSH deploy a pod with
`ssh=True` and run `lazycloud ssh <pod> --app <app>`. TCP pods need
`LAZYCLOUD_EDGE_TCP_ADDR`, `_URL`, `_CERT` and `_KEY` on the server.

## Progress

- [x] Contracts: migration 0011, OpenAPI, protobuf, Go dependencies
- [x] Supervisor
- [x] Agent
- [x] Server: execution, SSH, API, host session, edge
- [x] Python SDK and CLI
- [x] End to end on Docker with the real server
- [x] Measurements
