# R2 image archive cutover

Image archives use a private R2 bucket. Workspace buckets, source packages,
checkpoints and deployment bundles retain their existing stores. Workers use
short-lived signed S3 URLs. Merging this code does not migrate objects.

## Prepare

1. Confirm R2 is enabled on the deployment's Cloudflare account. Terraform reads
   its identifier from the existing tunnel credentials. The operator's
   `CLOUDFLARE_API_TOKEN` needs R2 storage write permission on that account.
   Do not widen the application's custom-hostname token for this purpose.
2. Review the Terraform plan. It must add `<deployment>-image-archives`, its
   incomplete-upload lifecycle, disabled public domain and descriptor version 3. It must
   not replace or delete any S3 bucket. Apply only the reviewed infrastructure
   change. Do not deploy the new application settings yet. The R2 bucket has
   `prevent_destroy`; its `enam` location is a hint, not a residency guarantee.
3. Create an R2 object read/write credential scoped to that archive bucket.
   Add `LAZYCLOUD_IMAGE_ARCHIVE_BACKEND__ACCESS_KEY_ID` and
   `LAZYCLOUD_IMAGE_ARCHIVE_BACKEND__SECRET_ACCESS_KEY` to the deployment's
   operator secret document, preserving all existing properties. Terraform owns
   no credential value. Only the control plane and scheduler receive these keys.
4. Keep R2 public access disabled and attach no public custom domain. Verify
   unsigned downloads fail. Signed URLs use `r2.cloudflarestorage.com`.
   R2 does not implement S3 versioning or public-access-block APIs. See
   [R2 compatibility](https://developers.cloudflare.com/r2/api/s3/api/).
5. Inventory the deployed source archive bucket and prefix. Record the original
   application revision and non-secret storage settings for recovery. Preserve
   source objects and versions through the reviewed recovery window.

## Copy and verify

Create an owner-readable-only configuration file with these fields. Values below
are placeholders. An AWS source can omit keys and use the operator's AWS
`default` profile. Supply `LAZYCLOUD_DATABASE_URL` and
`LAZYCLOUD_DATABASE_DIRECT_URL` securely; the command uses the direct connection.

```json
{
  "source": {
    "bucket": "DEPLOYMENT-objects",
    "endpoint_url": null,
    "region_name": "us-east-1"
  },
  "target": {
    "bucket": "DEPLOYMENT-image-archives",
    "endpoint_url": "https://ACCOUNT_ID.r2.cloudflarestorage.com",
    "region_name": "auto",
    "force_path_style": true,
    "access_key_id": "R2_ACCESS_KEY",
    "secret_access_key": "R2_SECRET_KEY"
  },
  "prefix": ""
}
```

Run the new CLI from the reviewed revision before deploying its chart:

```sh
uv run --group workspace lazycloud-admin archive-migration run --configuration /secure/archive-migration.json --operation copy
uv run --group workspace lazycloud-admin archive-migration run --configuration /secure/archive-migration.json --operation verify
```

The service walks durable archive rows, downloads source bytes and verifies their
size and SHA-256. It copies missing target objects, preserves keys and metadata,
and downloads the target bytes to verify them independently. Rerunning verifies
previous copies and continues missing ones. Conflicting target objects stop the
command rather than being overwritten. It never deletes source or target objects.
Finish any already-claimed retention cleanup before retrying.

Only one migration runs per database. Its transaction holds an advisory lock;
another copy, verification or cutover fails immediately. Keep the destination
exclusive to this migration until cutover, including manual object writes.

Progress goes to stderr. The final result uses normal CLI output formatting.
Temporary disk space must fit two copies of the largest archive. Budget S3
egress for repeated source verification. Managed multipart uploads use the
production S3 client; abandoned uploads expire after one day.

## Switch

1. Stop new image builds and checkpoint publication, drain in-flight archive
   uploads, and stop archive retention. Suspend deployment reconciliation that
   would restart old services, then stop the control plane and scheduler.
   Inspect worker processes and durable build records. An already issued upload
   capability remains usable after the API stops.
2. Confirm every issued upload URL has expired using the deployed presign
   duration, normally 15 minutes, and every in-flight upload has finished.
   Poll worker and object-store signals during this interval. Keep publication
   and retention stopped until rows and application settings agree.
3. Run `copy` again, then:

   ```sh
   uv run --group workspace lazycloud-admin archive-migration run --configuration /secure/archive-migration.json --operation cutover --writers-stopped
   ```

   Cutover verifies both stores again and updates archive bucket coordinates in
   one transaction. An exclusive table lock fences competing row mutations;
   failure rolls every coordinate update back. `--writers-stopped` confirms
   external writers have stopped. A database lock cannot revoke signed URLs.
4. Deploy the new chart using descriptor version 3. Preserve any nonempty prefix
   in `runtime.LAZYCLOUD_IMAGE_ARCHIVE_PREFIX`. Remove legacy archive bucket or
   signing-endpoint overrides before selecting the separate backend. Resume
   deployment reconciliation and services only after the new settings are ready.
5. Prove real image upload, wrong-checksum rejection, signed download, multipart
   cleanup and a cold worker image pull followed by workload execution. Verify
   retention on a disposable archive without touching sibling images. Retain
   S3 source data through acceptance and the reviewed recovery window.

## Recover

Before cutover commit, failure leaves all coordinates in the source store.
Fix the reported failure and retry, or restart the original deployment.

After commit, stop publication and retention again, drain uploads and expire
capabilities. Swap source and target in a protected copy of the configuration.
Run `copy`, then `cutover --writers-stopped` to bring newly created archives
back before restoring the original application settings. Conflicting bytes
require review. Keep services stopped throughout coordinate and deployment
rollback, then verify a cold image pull before reopening.

Retaining the original S3 bucket protects migration source versions only. It is
not a backup of archives created after cutover. Do not remove source versions or
claim equivalent version recovery without a separately reviewed backup policy.
Workspace data migration is a later change.
