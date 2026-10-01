# Workloads packet

Parity sections "Pods and devboxes", "Sandboxes" and "Shells and SSH" from
tasks/parity.md, plus the items handed off by endpoints and observability:
`checkpoint_enabled` and memory snapshots, `lazycloud container checkpoint`,
endpoint shells, and the dashboard APIs those pages need. Migration
`migrations/0011_workloads.sql`, protobuf fields 90-99, OpenAPI operations
tagged `workloads`.

## Outcome

`lazycloud deploy tools:web` runs a pod whose URL answers through the edge,
wakes on the first request and stops after `keep_warm` idle seconds;
`web.scale(2)` holds two. `web.create()` starts an instance with a URL and
`terminate()`. `lazycloud devbox box ssh` wakes a devbox, shows its phases
and opens a root shell over a certificate; `ssh-config` makes plain `ssh`
and editors work. `sandbox.create()` gives an instance whose processes,
files, ports, network policy, TTL, filesystem image and memory snapshot
work as in the reference. `lazycloud shell` and the dashboard shell open a
PTY in any container.

## Design

- One container model. Pods (kind `pod`, devboxes are pods with
  `pod.kind = devbox`) and sandboxes (kind `sandbox`) are workloads whose
  release spec carries a `pod` section; execution owns their containers like
  any other. `containers.purpose` separates the containers planning counts
  (`serve`) from those started on request (`instance`: `Pod.create`,
  `Sandbox.create`; `shell`: standalone shells). Planning never counts,
  drains or replaces an instance.
- Idle. `containers.active_until` is when a container stops being active;
  activity (an API call on it, a lease ending) pushes it to
  `now + keep_warm_seconds`. `container_leases` rows are open connections
  (shells, SSH, tunnels, HTTP in flight), renewed by their holder; a dead
  holder's lease expires. A container is idle when `active_until` passed and
  no lease is live. The scheduler stops idle instances; planning stops idle
  pod containers above the pod's count. This replaces the reference's Redis
  keep-warm locks and connection counters, which never expired.
- Pods. Without a scale a pod runs one container while it has connections or
  was woken in the last 15 minutes, and keeps it until it is idle;
  `keep_warm = -1` keeps one always. `pod_states.replicas` (scale) holds a
  count. A connection to a cold pod wakes it (`woken_at`) and waits for the
  container-ready notification. A stopped devbox is parked until the next
  connection or Start.
- Host. The supervisor is PID 1 everywhere. In pod mode it runs the command
  instead of runner slots and is ready once the command runs and its health
  check (or its first port) answers. Every supervisor serves the control API
  below on `control.sock` in the link directory; the agent reaches it from
  the data connection (`RequestHead.control`, `RequestHead.port`), so
  processes, files, shells, SSH and port traffic reuse the endpoints
  packet's edge-to-agent transport. The framed shell server and HMAC
  passwords are gone: a shell is a PTY process over a WebSocket.
- SSH. The workspace's user CA (`ssh_authorities`) and each pod's host key
  (`ssh_host_keys`) are ed25519 keys sealed by the secrets owner, created on
  first use. Certificates last 12 hours for principal `root`. The CLI
  bridges `ssh` through `GET .../apps/{app}/pods/{pod}/ssh` (WebSocket); the
  server opens a tunnel to the supervisor's SSH server over the data
  connection.
- Network policy. The agent applies `block_network`/`allow_list` as an
  nftables netdev egress filter on the container's interface, from a helper
  container that joins the container's network namespace with NET_ADMIN.
  Updates arrive as `UpdateNetwork`.
- Snapshots. `SnapshotContainer` makes the agent run `docker checkpoint
  create --leave-running` (runsc or CRIU), archive the checkpoint, upload it
  to a presigned URL and report `CompleteSnapshot`. A start with `restore`
  creates the container and starts it from the checkpoint. Automatic
  snapshots (`checkpoint`) are taken from the first ready container of a
  release and restore later cold starts, falling back to a cold start.
- Filesystem images. `PublishFilesystem` makes the agent stream the
  supervisor's tar of the root filesystem without its mounts into `docker
  import`, push it to the workspace image repository and report
  `CompleteFilesystemImage`; the image is registered with the images owner.
- Devboxes. The root disk is a storage disk declared at `/`. The agent
  mounts it at `/lazycloud/root`; the supervisor seeds it from the image on
  first use, binds `/proc`, `/dev`, `/sys`, `/run/lazycloud`, `/etc/hosts`,
  `/etc/resolv.conf` and the volumes into it, and runs the command and every
  session with it as root.

## Supervisor control API

HTTP/1.1 on `control.sock` (`Configure.control_socket`, in the link
directory). Bodies are the public schemas in contracts/openapi.yaml; errors
are the `Error` schema (`not_found`, `invalid_request`, `conflict`,
`payload_too_large`, `unavailable`). The server forwards the public
operation under `/v1/workspaces/{ws}/containers/{c}` to the same path here,
so status and body pass through unchanged.

| Request | Public operation |
| --- | --- |
| `GET /processes`, `POST /processes` | listProcesses, startProcess |
| `GET /processes/{id}?wait_seconds=`, `POST /processes/{id}/kill` | getProcess, killProcess |
| `GET /files?path=&limit=`, `DELETE /files?path=`, `GET /files/stat?path=` | listContainerFiles, deleteContainerFile, statContainerFile |
| `GET /files/content?path=&max_bytes=&truncate=`, `PUT /files/content?path=&mode=` | downloadContainerFile, uploadContainerFile |
| `POST /files/find`, `POST /files/replace` | findInContainerFiles, replaceInContainerFiles |
| `POST /directories?path=&mode=`, `DELETE /directories?path=` | createContainerDirectory, deleteContainerDirectory |
| `GET /shell?cols=&rows=&term=` (WebSocket) | openContainerShell |
| `GET /ssh`, `Upgrade: lazycloud-tunnel` → 101, then SSH bytes | openSshTunnel |
| `GET /ports/{port}`, `Upgrade: lazycloud-tunnel` → 101, then TCP bytes to 127.0.0.1:port | pod and sandbox ports |
| `POST /filesystem` → `application/x-tar` of `/` without mounts | createFilesystemImage |

Processes keep the reference's limits: 256 KiB retained per stream, a 64 MiB
budget for all output, results kept 5 minutes after exit, at most 65,536
records, a one-second output drain after exit, exit code 128+N for signal N.
Relative paths are under `/workspace`. Downloads stop at `max_bytes`
(default and cap 64 MiB); listings at `limit` (default and cap 10,000).

## Work split

| Stream | Owns |
| --- | --- |
| Supervisor | `internal/supervisor`, `cmd/supervisor`: pod mode, readiness, Docker daemon, devbox root, control API, PTY shell, SSH server (from the reference's ssh.go), port tunnels, filesystem tar, `netfilter` subcommand |
| Agent | `internal/agent`, `cmd/agent`: pod starts, control and port forwarding on the data connection, network policy helper, checkpoint and restore, filesystem publish, devbox disk mount, incremental startup stages |
| Python | `python/lazycloud`: SDK and CLI on the new API, unsupported options removed |
| Integrator | migration, contracts, execution, control, api, hostsession, edge, images and secrets hooks, merges and end-to-end acceptance |

## Progress

- [x] Contracts: migration 0011, OpenAPI, protobuf, Go dependencies
- [ ] Supervisor
- [ ] Agent
- [ ] Server: execution, SSH, API, host session, edge
- [ ] Python SDK and CLI
- [ ] End to end on Docker with the real server
- [ ] Measurements
