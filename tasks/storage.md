# Storage: volumes, disks, artifacts, queues and maps

Packet storage from tasks/wave-2.md. Migration `migrations/0007_storage.sql`,
proto fields 40-49, OpenAPI operations tagged `storage`.

Outcome: the parity sections "Volumes and disks", "Artifacts" and "Maps and
queues" from tasks/parity.md, on PostgreSQL and the object store, with the
same SDK classes and CLI commands as the reference.

## Design

PostgreSQL holds metadata and authority; the object store holds bytes.

- Every workspace gets its own bucket for volumes and disks
  (`<prefix>-<workspace id hex>`), created on first use. That is the only
  scope both Garage and S3 can grant a key to: Garage keys are allowed per
  bucket, and STS session policies name the bucket. The reference made the
  same choice. Artifacts and sources stay in the platform bucket.
- Hosts get a `StorageGrant` over the session: a one-hour key for one
  workspace bucket, sent before the first start that needs it and again 30
  minutes before it expires. Garage keys come from the admin API with an
  expiry and are deleted by the sweep once expired (`storage_grants`); AWS
  uses STS AssumeRole with a session policy on the bucket.
- The agent runs one GeeseFS mount per workspace (`bucket:volumes/`) inside a
  small mount container that holds the credentials, and bind-mounts each
  volume's directory into workload containers. Workloads see no credential.
  The mount container shares its mount through an rshared bind, so the same
  code works for root and unprivileged agents and keeps running across agent
  restarts. GeeseFS reads its key through `credential_process = cat`, so a
  new grant takes effect without remounting.
- `volume_mounts(volume, container)` is written when the server builds the
  start. The mount locks the volume `FOR SHARE`, a delete locks it `FOR
  UPDATE` and refuses while any mounting container has not stopped. Deleting
  frees the name at once; the sweep removes the files, then the row.
- Disks keep the reference disk engine, ported to `internal/diskengine` as a
  library the agent calls. The lease row carries the holder container and a
  32-byte token; every generation record is fenced by the token, the calling
  host and the holder's state. The holder keeps the disk until it releases
  it after the final publish, or its container stopped with its host lost.
- Artifacts are rows plus objects at `workspaces/<ws>/artifacts/<id>`. Up to
  64 MiB uploads with one presigned PUT whose signed length makes the store
  refuse other bytes; larger ones are multipart. Completion checks the
  stored size, then starts retention.
- Queues are `queue_messages` rows; pop is `DELETE ... FOR UPDATE SKIP LOCKED
  RETURNING`, and a pop with `wait_seconds` waits on `NOTIFY lc_queue`.
  Maps are `map_entries` rows with a revision from one sequence for compare
  and set, and an `expires_at` that reads treat as missing and the sweep
  deletes.
- The scheduler runs the storage sweep every 30 s: expired and abandoned
  artifacts, deleted volumes and disks, expired map entries, expired keys and
  volume sizes older than 10 minutes. Each step claims bounded batches with
  SKIP LOCKED, and one item's failure leaves it for the next pass.

## Choices

- GeeseFS 0.43.9 over mountpoint-s3. Volumes are general filesystems in the
  reference: renames, appends and overwrites all work. mountpoint-s3 refuses
  renames on plain S3, appends and in-place writes. The reference used GeeseFS
  for volumes too. The binary is pinned by digest in
  `deploy/local/fetch-geesefs.sh`.
- The mount container runs a pinned busybox image, which supplies `sh`,
  `cat`, `mkdir` and `umount` beside GeeseFS. Its script detaches the mount
  when GeeseFS ends, because GeeseFS cannot unmount itself there and a dead
  FUSE mount would break every workload bound to it.
- Retention: `Storage.ArtifactRetention` returns the Free plan's 1 day for
  every workspace. Billing owns plans; Team (30 days) and Business (90) need
  that one function to read the plan once billing lands.
- `github.com/aws/aws-sdk-go-v2/service/sts` v1.51.1 is the one new Go
  dependency, for AWS grants.

## Intentional differences from the reference

- Volume file paths are query parameters, not URL suffixes. In the reference,
  `lazycloud rm lazycloud://vol` hit the delete-volume route and deleted the
  whole volume; a directory named `stat` could not be listed. Removing the
  root is now refused.
- Volume names follow `^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$`. The reference
  accepted any name, and names with `/` could never be reached.
- A deleted volume's name is free at once, so the volume list has no
  "deleting" status.
- Volume `size` comes from a periodic count, at most 10 minutes old. The
  reference scanned every volume's objects on each list.
- Directories report no modification time instead of "now".
- Uploads go straight to the object store through presigned URLs. The
  reference sent whole files base64-encoded in JSON with no size limit.
- Moves copy objects above 5 GiB in parts; the reference failed on them.
- An absolute mount path mounts once, at that path. The reference also
  mounted it under /volumes.
- `read_only` works for platform volumes; the reference ignored it.
- CloudBucket mounts are refused at deploy for now. Their keys are workspace
  secrets, which the workload-runtime packet provides. The host protocol and
  agent refuse them too until then.
- Artifacts: `delete` of a missing id is `not_found` (the reference returned
  204). Bulk delete is one request for up to 100 ids. `public_url` is capped
  at the artifact's remaining retention instead of failing near expiry, and
  download and preview use presigned URLs with the content type and
  disposition set, instead of the server reading whole objects into memory.
  Listing hides expired artifacts the sweep has not removed yet.
- Artifact usage has no cost fields until billing lands.
- Queues: a stored empty message differs from an empty queue (the reference
  returned empty bytes for both). Messages are at most 1 MiB. Queues have no
  put-rate statistic: counting puts on the queue row would serialize puts.
- Maps: keys are listed in byte order and pages never come back empty with a
  cursor. `if_absent` is atomic. The reference's key and name collisions
  through `:` are gone.
- `m[missing]` raises `KeyError` in the SDK (the reference returned None,
  which broke `MutableMapping`).
- Function `disk=` is the writable layer limit, sent as `resources.disk_mib`.
  Docker enforces it only on overlay2 over XFS with project quotas; other
  hosts log at startup that the limit is not enforced.

## Measurements

Owner level, real PostgreSQL 18 (compose.test.yaml, fsync off), 24 CPUs,
`go test -bench . -cpu 1,24 ./internal/storage/`:

| Operation | One client | 24 clients, aggregate |
| --- | --- | --- |
| Queue put (one message) | 0.17 ms | 29,000/s |
| Queue pop | 0.11 ms | 32,000/s |
| Map set | 0.19 ms | 28,000/s |
| Map get | 0.07 ms | 64,000/s |

A pop waiting with `wait_seconds` returns within milliseconds of the put
(TestQueuePopWaitsForPut).

## Tests

Go, against PostgreSQL, Garage and Docker:

- `internal/storage`: TestQueueDeliversEachMessageOnceInOrder,
  TestQueuePopWaitsForPut, TestQueueDeleteAndSizeLimit,
  TestMapCompareAndSet, TestMapExpiryKeysAndStats, TestVolumeFiles,
  TestVolumeMultipartUpload, TestVolumeDeletionChecksLiveMounts,
  TestHostGrantReachesOneWorkspace, TestArtifactLifecycle,
  TestArtifactMultipartUpload, TestDiskLeaseFencesHolders, benchmarks.
- `internal/hostsession`: TestStartSendsAWorkspaceGrantFirst,
  TestDiskLeaseOverTheHostConnection.
- `internal/agent`: TestVolumesMountThroughWorkspaceBucket (two containers
  share a volume through GeeseFS; the read-only one cannot write; neither
  sees a credential).
- `internal/diskengine`: chunking, manifests, credential refresh, publish and
  restore through Garage, seal, compact, recover, flatten and collect through
  qemu-storage-daemon. The attach test skips here: it needs root, nbd-client
  and the nbd module.

## Gaps

- Disks attach on hosts with root, `nbd-client` and the nbd module. This
  machine has none, so attach, mount and freeze are unverified, and no
  workload declares disks yet: they belong to pods, which no packet has
  built. The release spec field `disks` and `StartContainer.disks` are
  wired; root disks (`mount_path="/"`) need a rootfs the agent controls,
  which Docker does not give it.
- CloudBucket waits for workspace secrets.
- In-container SDK calls (artifact save, queues, maps from inside a task)
  use the profile token until the workload-runtime container API lands;
  then artifact creation is bound to the calling attempt.
- AWS grants (STS) and AWS bucket creation are untested; only Garage was
  available. The server role must allow `s3:*` on the workspace buckets and
  `sts:AssumeRole` on `LAZYCLOUD_WORKSPACE_BUCKET_ROLE_ARN`.
- Dashboard pages come with the web packet; their APIs are here.
- Retention by plan waits for billing.

## Progress

- [x] Schema, OpenAPI, Go owner and handlers
- [x] Host protocol, session grants, agent volume mounts
- [x] Disk engine port, disk leases, agent disk lifecycle
- [ ] Python SDK and CLI on the new API
- [ ] End to end through the SDK
