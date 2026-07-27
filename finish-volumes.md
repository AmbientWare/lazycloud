# Finish: volumes backed by per-workspace storage (#42)

## Goal

A Function declaring `volumes=[Volume("name", "/mnt/x")]` writes through the mount, and the client
reads that same content back. Today that read returns `HTTP 404`.

Done when `tests.e2e.local.storage.scenario_volume_mount --live` passes.

**Work to completion. Commit after each stage, do not stop at commit boundaries to report progress,
and if the live run fails, fix it and re-run rather than handing back a diagnosis.**

Branch `fix/volume-mount-worker`, continuing from `5fa9b74`. Reference implementation is cloned at
`../beta9` (Go) — cite it when a decision is non-obvious.

## State

**Write path: done.** A `Volume` mount now resolves onto the workspace's own storage mount
(`/workspace/<workspace>/volumes/<volume_id>`), reusing the rewrite `/outputs` already uses in
`adjust_mount_for_workspace_storage` (`packages/worker/src/worker/lifecycle.py`). The platform-wide
volume store, its security gate, and both workspace-storage flags are deleted.

**Read path: not done.** `packages/storage/src/storage/volume_filesystem.py` still addresses a single
platform bucket. So a worker write goes to the workspace bucket while a client read looks in the
platform bucket. Net user-visible behaviour is unchanged from before the work (both 404) — nothing
regressed, but the scenario cannot pass.

Baseline to compare against: 0 type errors on changed owners; `uv run --group dev pytest packages -q`
gives **17 failures, identical to HEAD**. Those 17 are pre-existing — verified by stashing and
re-running. Do not chase them.

## Why per-workspace (do not redesign this)

Volumes live inside the workspace's **own** bucket, as in beta9. Credentials are therefore
per-workspace and are already vended per container, so a worker only holds access to the workspaces
whose containers it runs. That is what lets volumes work on customer compute with no gate.

The rejected alternative was a platform-wide store: mounting it needs the metadata URI plus backing
bucket keys, which together reach every workspace's data, so it could never be given to
customer-operated hardware.

mountpoint-s3 was also rejected: it has no rename, no append, and no random writes, and defers errors
to `close()`. Users get a mounted directory and will do all three. beta9 uses a FUSE filesystem for
the same reason.

`AGENTS.md` now carries the rule this follows: **use the single best production route** — no flags,
gates, or fallbacks that let test and production diverge or silently degrade. Honour it below;
several deletions exist only because of it.

---

## Stage 1 — read path

### 1a. Add `copy` to the S3 client

`packages/storage-client/src/storage_client/s3.py` has **no copy operation**. Add one (boto3
`copy_object`) plus its entry on the client protocol. Needed by rename below.

### 1b. Resolve a client per workspace

`packages/storage/src/storage/volume_filesystem.py`, `JuiceFsGatewayVolumeFilesystem` (~line 285) is
a `@dataclass(slots=True)` holding `client`, `rename_client`, `bucket`, built once per process from
one credential set via `from_settings`.

Replace the fixed `client`/`bucket` with a resolver keyed by workspace, so every method starts by
resolving `(client, bucket)` for `namespace.workspace_id`. Cache per workspace — this is called on
every volume operation.

Credentials come from `WorkspaceRecord.storage` (`packages/shared/src/shared/identity.py:67`).
`workspace_storage_credentials` (`packages/worker-repository/src/worker_repository/credentials.py:337`)
already converts that into endpoint/region/bucket/keys — **reuse it; do not write a second reader.**

`packages/storage` must not import `worker-repository`. Define a narrow resolver protocol in
`volume_filesystem.py` and have the composition roots supply the implementation:

- `apps/api/src/api/server/services.py:213`
- `apps/scheduler/src/scheduler_app/services.py:205`

Rename the class to something honest (`WorkspaceVolumeFilesystem`) — it is no longer a JuiceFS
gateway client.

### 1c. Drop the workspace segment from the key

`_volume_root_key` is currently `volumes/<workspace_id>/<volume_id>`. With a per-workspace bucket the
workspace segment is redundant: it becomes `volumes/<volume_id>`, matching what the worker's mount
exposes and beta9's `pkg/abstractions/volume/volume.go:311`.

This is the identity the whole design rests on: **the presigned URL and the container's bind must
address the same object.** If the scenario 404s after this stage, check this first.

### 1d. Rename becomes copy + delete

`move_path` currently calls `self.rename_client.move_path(...)` — a WebDAV `MOVE` against a
platform-wide endpoint. Per-workspace buckets have no such endpoint. beta9 does server-side copy then
delete (`volume.go:586-587`). Do the same, keeping the existing
`"a directory cannot be moved inside itself"` guard.

Then delete `JuiceFsWebDavClient` and the `VolumeRenameClient` protocol.

Note: rename is no longer atomic. beta9 accepts this. Check no caller depends on atomicity.

### 1e. Verify and commit

```sh
uv run --group dev ruff check --fix packages apps
uv run --group dev basedpyright packages/storage packages/worker packages/execution apps/api apps/scheduler
uv run --group dev pytest packages -q          # expect the same 17 pre-existing failures
```

Commit before continuing.

---

## Stage 2 — live run

```sh
docker compose -f compose.yaml up -d --build
uv run python -m tests.e2e.local.compose.readiness
uv run python -m tests.e2e.local.storage.scenario_volume_mount --live
```

**Expect this to fail the first time.** Workspace storage has never actually run — it was flag-gated
off since it was written, so expect real defects in `WorkerWorkspaceStorageManager`
(`packages/worker/src/worker/workspace_storage.py`), not just wiring. Fix and re-run until green.

Confirm the mount is real, the inverse of the observation that diagnosed the bug:

```sh
docker exec <worker> sh -c "grep /workspace /proc/self/mountinfo"   # expect a FUSE mount
docker exec <worker> sh -c "mount | grep /workspace"                # must NOT be plain ext4
```

Known traps waiting in this stage:

- **`workspace_storage.py:185` formats on every mount**, and `filesystem_name` is a single shared
  constant while each workspace has its own bucket. Two workspaces would collide on one filesystem.
  Derive the name from the workspace.
- **`juicefs mount` is invoked without `allow_other`** (`storage_client/mounts.py:155`) while
  `mountpoint_command` passes `--allow-other` (line 277). Non-root containers may get `EACCES` across
  the bind. Add `-o allow_other` if so.
- **An agent slot's `juicefs mount` process started but never completed** in earlier testing — `/data`
  stayed empty and never appeared in `mountinfo`, and the worker logged nothing. If that recurs,
  surface the mount process's stderr first; `JuiceFsMountManager` starts it as a managed background
  process and its output currently reaches no log.

Then the remaining storage scenarios, which must not regress:

```sh
uv run python -m tests.e2e.local.storage.scenario_volume_cli --live
uv run python -m tests.e2e.local.storage.scenario_volume_transfer --live
uv run python -m tests.e2e.local.storage.scenario_cloud_bucket --live
uv run python -m tests.e2e.local.storage.scenario_output --live
```

`scenario_output` matters especially: removing the flags changed `/outputs` behaviour too, and that
is the same latent bug one directory over.

Commit once green.

---

## Stage 3 — deletions

**Only after stage 2 is green.** The read path uses these until then.

- `juicefs-webdav` service (`compose.yaml`) and its chart container — existed only for rename
- `juicefs-gateway` — served the platform filesystem as S3 for presign; presign now goes to the
  workspace's own bucket
- `juicefs-format` — formatted the platform filesystem; workspace storage formats per workspace
- `x-juicefs-gateway-env` anchor, `JuiceFsGatewaySettings`, and `LAZYCLOUD_JUICEFS_GATEWAY_*` across
  `control-plane`, `cli`, `scheduler`
- `LocalVolumeFilesystem` (`volume_filesystem.py`) — **no production wiring**; both composition roots
  build the real implementation unconditionally. Used only by
  `packages/storage/tests/test_volume_object_storage.py` and `test_volume_metering.py`, which must be
  reworked onto the real implementation. This is precisely the test-differs-from-production shape the
  new rule bans, which is why it goes.
- `ImageArchiveStorageMode` — production always passes `Local` (`production.py`); other values are
  never taken
- `StorageMountMode.Local` — defended against at four sites yet still constructible; remove the member
  so the state is unrepresentable

**Keep `juicefs-redis`** — workspace storage still needs a metadata engine.

Re-run the full storage suite after deleting, then commit.

---

## Stage 4 — close out

- Comment on #42 with the evidence: the scenario output, the `mountinfo` check, and what the first
  live run turned up.
- Move #42 to `Merged` on the Agent Development board after its PR merges (project 7; the agent moves
  its own ticket, never skipping a column).
- #38 (workspace secrets, `finding` label) is blocked on this. Its remaining acceptance is the storage
  scenarios above, so once they pass, finish #38 and check off **F-20**. That leaves F-16 (#29) as the
  only open finding.

## Assumptions worth flagging if wrong

- **Existing volume data is abandoned.** Volumes move from the shared `lazycloud-data` bucket to
  per-workspace buckets. Predeployment with AWS at verified zero, so the assumption is recreate, not
  migrate.
- **`STORAGE_AVAILABLE` reaches user containers** (`execution.py:477`). Making storage unconditional
  changes what user code sees; check nothing branches on it.
