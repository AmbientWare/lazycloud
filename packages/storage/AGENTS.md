# Storage package

Protocol-backed object and cache storage, S3-compatible clients, mounted storage,
and the durable records that describe stored objects.

API and CLI formatting, scheduler and worker loops, providers, duplicate
persistence, and local JSON stores stay out.

Volume metering is billable, so a metering window is priced in the transaction
that records it rather than by a later pass. A record committed without its cost
is money this platform measured and can no longer charge for, and the usage row
alone cannot say whether the cost was skipped or never owed.

A durable disk is a sized block device with its own filesystem, for stateful
workloads that a shared volume mount does not fit. This package owns its record
and its lease. A disk is named in its workspace and one container holds it at a
time. Every publish carries the holder's lease token, so a container that lost
the disk cannot publish over the one that holds it. The holder keeps the lease
until its last publish and release land, or until its worker is gone, because
releasing earlier lets the next container start from a generation the holder was
about to replace. Chunks and manifests live under `disks/<id>/` in the workspace
bucket, and deleting a disk removes that prefix and nothing beside it. After a
self-contained generation is recorded, its holder deletes the chunks and
manifests only older generations used. The same chunks serve a restore on any
machine: the engine fetches each on first read, so a start does not wait for
the disk to download. The prefix also holds a small heat map naming the chunks
the last session read; it is a hint and bills nothing.

On a provider machine each disk gets its own block volume, created in the
machine's zone at `disk_volume_size_bytes` of the declared size and attached for
as long as a container holds the disk. Released, it stays detached for
`DISK_VOLUME_CACHE_SECONDS` so a restart in the same zone reuses it, then it is
deleted. A joined machine gets no volume and keeps disks in host storage.

The disk row records the volume's state. Provider calls run outside
transactions, and each result is recorded only if the row still has the
revision and lease it was read under. That fence lets the holder, the
housekeeping sweep and deletion drive the same volume and resume each other's
half-finished work. A housekeeping decision stops the moment the lease or
revision it read changes, so a sweep never detaches a volume a new holder just
took. Only the driver that moved a volume into a state makes that state's
provider call, until it has plainly stopped. An acquire that finds the work
unfinished answers `DiskVolumePendingError`, and the worker asks again under the
same lease. A call the driver made is recorded whatever the lease is by then.

A driver claims its state again before every provider call, and the claim moves
the revision. A driver has plainly stopped once it has gone longer without a
claim than its own wait plus a margin: an acquire's 20 seconds for a lease, the
sweep's 120 for housekeeping and deletion. The lease bound has to sit well
inside the worker's acquire deadline, or a new holder that finds a dead lease's
attach unfinished times out before it may take over. Housekeeping that only
looks at a volume never counts as a claim.

Deleting a disk through the API records the intent and returns. The scheduler's
deletion sweep detaches and deletes the volume, removes the objects and then the
rows. A failed attempt pushes the disk's next attempt out by its age, so a
deletion that keeps failing is tried less often and never holds up newer ones.

A creation abandoned part way leaves a record outside the disk row naming its
account, region and token. Orphan collection uses it to find the volume the
creation may have made, and the AWS connection cannot be disconnected until
that record is gone, even after the disk and its workspace are. Every volume
carries the deployment's tags, and orphan collection deletes only detached
volumes with this deployment's complete tag set that no disk row names.

This package handles user data, so its invariants are the ones whose failure
cannot be undone. Validate paths and keys against traversal, verify checksums,
track multipart state, scope credentials and presigned URLs narrowly and briefly,
keep concurrent writers from corrupting each other, preserve sibling objects on
every delete, and clean up what a failed operation left behind.

Volume deletion records intent before calling storage. That instant ends customer
metering. The volume remains visible and owned until housekeeping finishes cleanup;
container admission and file writes lock the same volume row as deletion.

Presigned volume writes use multipart uploads whose completion goes through the
API. Direct PUT requests can finish after their URL expires, so volumes that may
have issued those URLs retain an unmetered cleanup record for their exact ID.
Recreating a name gets a different ID. Retiring the workspace's bucket removes
those records, wherever the bucket lives.

A workspace's bucket lives where the workspace does. A workspace with no
connection has a bucket in the platform's object store, reached through the
deployment's own credential. A workspace created in a connected AWS account has
its bucket in that account, and the platform holds no standing credential for
it: the router asks the connection's role for a session narrowed to that one
bucket, workloads mount with that session, and the volume store re-resolves a
store whose session is about to lapse. The bucket is deleted with the
workspace. Nothing here moves a workspace between the two.

An empty or negative credit balance starts a 30-day managed-storage grace
period and queues an email in the same transaction. Its timestamp is durable;
another sweep cannot restart it. Replenishment before a deletion claim closes
the period. Storage charges
inside a grace period are waived through the billing ledger's settlement owner.

After grace, claim individual objects and volume deletion intents under the
same account lock that protects credit issuance. Skip busy resource locks while
holding that lock because metering takes its resource lock before billing.
Existing cleanup owns retries and late-write cleanup. Never retire a workspace,
delete a whole bucket, or touch a workspace that lives in a connected account
for exhausted credit; the customer pays their own provider for that storage.
