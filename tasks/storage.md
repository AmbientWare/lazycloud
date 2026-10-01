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
  uses STS AssumeRole with a session policy allowing object reads, writes,
  multipart uploads and listing under `volumes/` and `disks/` only. Garage
  can scope a key to a bucket but not to a prefix.
- The agent runs one GeeseFS mount per workspace (`bucket:volumes/`) inside a
  small mount container that holds the credentials, and bind-mounts each
  volume's directory into workload containers. Workloads see no credential.
  The mount container shares its mount through an rshared bind, so the same
  code works for root and unprivileged agents and keeps running across agent
  restarts. GeeseFS reads its key through `credential_process = cat`, so a
  new grant takes effect without remounting. The mount directory is 0700
  and owned by the agent. Mount containers have memory and process limits;
  the agent watches each one, and if one exits on its own, every container
  using it is stopped and reports why, instead of running on a dead mount.
- A cloud bucket mounts per container in its own mount container, with the
  keys of the two workspace secrets its spec names, resolved when the start
  is built. A missing secret fails the start like a missing release secret.
- `volume_mounts(volume, container)` is written when the server builds the
  start. The mount locks the volume `FOR SHARE`, a delete locks it `FOR
  UPDATE` and refuses while any mounting container has not stopped. Deleting
  frees the name at once; the sweep removes the files, then the row.
- Disks keep the reference disk engine, ported to `internal/diskengine` as a
  library the agent calls. The lease row carries the holder container and a
  32-byte token; every generation record is fenced by the token, the calling
  host and the holder's state. Manifest keys carry the manifest digest, so
  a stale holder cannot replace a recorded manifest. The holder keeps the
  disk until it releases it after the final publish, or until its host is
  lost or retired. The agent records leases in a host-wide lease directory
  before attaching, and a release loop publishes, detaches and releases
  every lease of a container that no longer runs, retrying each minute and
  across agent restarts; a disk the host never restored is released
  without a publish.
- Artifacts are rows plus objects at `workspaces/<ws>/artifacts/<id>`. Up to
  64 MiB uploads with one presigned PUT whose signed length makes the store
  refuse other bytes; larger ones are multipart. Completion checks the
  stored size, then starts retention.
- Queues are `queue_messages` rows; pop is `DELETE ... FOR UPDATE SKIP LOCKED
  RETURNING`, and a pop with `wait_seconds` waits on `NOTIFY lc_queue`.
  Maps are `map_entries` rows with a revision from one sequence for compare
  and set, and an `expires_at` that reads treat as missing and the sweep
  deletes.
- In-container calls (queues, maps, artifacts) go through the container
  API with the container's principal. An artifact saved there belongs to
  the task whose attempt runs on the container; the body cannot name
  another.
- The scheduler runs the storage sweep every 30 s: expired and abandoned
  artifacts, deleted volumes and disks a chunk of 1,000 objects at a time,
  expired map entries, expired keys, volume sizes older than 10 minutes, and
  once an hour per workspace, objects no row owns (written by an upload URL
  or a host key that outlived a delete). Write URLs last at most an hour.
  No step holds a transaction across object store calls, and one item's
  failure leaves it for the next pass.

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
- A CloudBucket needs both key secrets. The reference's ambient mode, which
  used a connected AWS account's node role, waits for connected clouds.
- Queue and map listings count at most 100,001 messages or keys per item;
  getting one queue or map counts exactly.
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
- `Volume.file_service_info()` returns a constant; there is no endpoint.
- `lazycloud volume list` shows name, size and created. The reference's
  status and updated columns are gone: a deleted volume leaves the list at
  once, and volumes have no update time.
- A `Disk` size must be whole 4096-byte blocks; the reference accepted any
  byte count and then failed at attach.
- List pages default to 50 entries, the identity packet's shared `Limit`.

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

Through the public API (server on this host, one client over HTTP/1.1
keep-alive, 500 calls each, PostgreSQL 18 on tmpfs):

| Call | p50 | p95 |
| --- | --- | --- |
| Queue put | 0.49 ms | 1.34 ms |
| Queue pop | 0.49 ms | 1.18 ms |
| Map set | 0.60 ms | 1.56 ms |
| Map get | 0.76 ms | 1.11 ms |

The reference kept queues and maps in Redis behind the Python API; its
numbers on this host are not measured here.

End to end through the SDK (test_function_writes_into_a_mounted_volume):
deploying a function with `volumes=[Volume(name)]`, a cold `.remote()` that
writes a file under the mount, and reading the file back from outside with
`Volume.read_text` takes 2.2 to 2.4 s. A workspace's first mount adds about
0.3 s to container start (start stage 0.50 s against 0.19 s with the mount
already up).

## Tests

Go, against PostgreSQL, Garage and Docker:

- `internal/storage`: TestQueueDeliversEachMessageOnceInOrder,
  TestQueuePopWaitsForPut, TestQueueDeleteAndSizeLimit,
  TestMapCompareAndSet, TestMapExpiryKeysAndStats, TestVolumeFiles,
  TestVolumeMultipartUpload, TestVolumeDeletionChecksLiveMounts,
  TestHostGrantReachesOneWorkspace, TestArtifactLifecycle,
  TestArtifactMultipartUpload, TestDiskLeaseFencesHolders,
  TestStaleHolderCannotReplaceARecordedManifest,
  TestHolderOnALostHostReleasesTheDisk,
  TestDeletingTheHolderContainerEndsTheLease,
  TestHostPolicyReachesOnlyVolumeAndDiskObjects,
  TestMoveKeepsNamesThatLookEscaped, TestSweepDeletesLargePrefixesInChunks,
  TestSweepRemovesObjectsWithoutOwners, TestWriteURLsAreShortLived,
  benchmarks.
- `internal/hostsession`: TestStartSendsAWorkspaceGrantFirst,
  TestCloudBucketKeysComeFromWorkspaceSecrets,
  TestContainerStorageCallsActForTheirTask,
  TestDiskLeaseOverTheHostConnection.
- `internal/agent`: TestVolumesMountThroughWorkspaceBucket (two containers
  share a volume through GeeseFS; the read-only one cannot write; neither
  sees a credential; the mount directory is 0700; a dead mount stops both),
  TestCloudBucketMountsWithItsKeys, TestDiskLeasesReleaseUntilAccepted.
- `internal/api`: TestStorageRoutes (escaped names and keys, typed errors,
  authorization) and TestSchemaPatternsCompile.
- `internal/diskengine`: chunking, manifests, credential refresh, publish and
  restore through Garage, seal, compact, recover, flatten and collect through
  qemu-storage-daemon. The attach test skips here: it needs root, nbd-client
  and the nbd module.

Python, `python/lazycloud/tests/test_storage_live.py` against a running
platform (skipped without `LAZYCLOUD_ENDPOINT`): volume files, multipart
put, the volume CLI with `cp` globs and downloads, disks, queues, maps,
artifacts with the artifact CLI, and a function writing into a mounted
volume, and a task using queues, maps and artifacts through the container
API. All 9 pass; the offline suite passes too.

## Gaps

- Disks attach on hosts with root, `nbd-client` and the nbd module. This
  machine has none, so attach, mount and freeze are unverified, and no
  workload declares disks yet: they belong to pods, which no packet has
  built. The release spec field `disks` and `StartContainer.disks` are
  wired; root disks (`mount_path="/"`) need a rootfs the agent controls,
  which Docker does not give it.
- Presigned artifact and volume URLs point at the server's object store
  endpoint, which containers must reach for in-container saves. Locally
  that means an endpoint on the Docker bridge, not 127.0.0.1.
- AWS grants (STS) and AWS bucket creation are untested; only Garage was
  available. The server role must allow object access on the workspace
  buckets and `sts:AssumeRole` on `LAZYCLOUD_WORKSPACE_BUCKET_ROLE_ARN`.
  GeeseFS under the narrowed session policy is untested on AWS. Without
  `LAZYCLOUD_OBJECT_STORE_ACCESS_KEY_ID` and `_SECRET_ACCESS_KEY` the server
  and scheduler use the AWS default credential chain (Pod Identity, IRSA,
  instance metadata); an empty endpoint means AWS S3 with virtual-hosted
  addressing. Resolution is unit-tested against the environment and a Pod
  Identity endpoint, not against AWS.
- Dashboard pages come with the web packet; their APIs are here.
- Retention by plan waits for billing.

## Progress

- [x] Schema, OpenAPI, Go owner and handlers
- [x] Host protocol, session grants, agent volume mounts
- [x] Disk engine port, disk leases, agent disk lifecycle
- [x] Python SDK and CLI on the new API
- [x] End to end through the SDK
