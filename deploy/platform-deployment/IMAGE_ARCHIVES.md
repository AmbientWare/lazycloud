# R2 image archive rollout

Image archives use a private R2 bucket. Workers upload and download through
short-lived signed S3 URLs. Terraform publishes the bucket and endpoint in
infrastructure descriptor version 3; the chart supplies a separate archive
backend to the control plane and scheduler.

The initial transition discards existing archives and rebuilds them in R2. This
module provisions the destination. Complete the scoped reset below before
deploying the new settings to an existing installation.

## Prepare the destination

1. Activate R2 on the deployment's Cloudflare account. Terraform reads its
   identifier from the existing tunnel credentials. The operator's
   `CLOUDFLARE_API_TOKEN` needs R2 storage write permission on that account.
   Keep the application's custom-hostname token separate.
2. Review the Terraform plan. It must add `<deployment>-image-archives`, its
   incomplete-upload lifecycle, disabled public domain and descriptor version 3.
   Apply only the reviewed infrastructure change. The bucket has
   `prevent_destroy`; its `enam` location is a hint, not a residency guarantee.
3. Create an R2 object read/write credential scoped to that archive bucket.
   Add `LAZYCLOUD_IMAGE_ARCHIVE_BACKEND__ACCESS_KEY_ID` and
   `LAZYCLOUD_IMAGE_ARCHIVE_BACKEND__SECRET_ACCESS_KEY` to the deployment's
   operator secret document, preserving its other properties. Terraform owns
   no credential value. Only the control plane and scheduler receive these keys.
4. Keep public access disabled and attach no public custom domain. Verify
   unsigned downloads fail. Signed URLs use `r2.cloudflarestorage.com`.
   R2 does not implement S3 versioning or public-access-block APIs. See
   [R2 compatibility](https://developers.cloudflare.com/r2/api/s3/api/).

## Coordinate the archive reset

Image archives can switch independently of workspace storage. Provision and
validate the archive destination before removing its source data. Apply the same
clean-reset approach to the remaining stores as their R2 consumers are ready.

Inventory `image_archives` and resolve each physical key using the current
archive prefix. Record its object versions, multipart uploads, image
authorizations and completed builds. Find the apps, deployments and tasks that
use those images. Keep a shared source bucket while other consumers still use it.
Terraform state, release artifacts and deployment configuration have separate
cutovers.

Pause affected apps through the public lifecycle API and resolve queued tasks.
Stop workload admission, image builds, checkpoint publication and retention.
Drain running work and storage mounts, suspend reconciliation that would restart
old services, and stop the control plane and scheduler. Confirm issued upload
and storage credentials have expired and in-flight transfers have ended. Poll
worker, database and object-store signals; stopping an API does not revoke a
previously issued capability.

There is no archive-only reset command. With writers stopped, use the image
repositories to delete the inventoried build records,
workspace image authorizations and archive rows in one transaction, asserting
the expected IDs and counts. Build logs and request mappings cascade with their
build. Completed builds must be cleared too: fingerprint reuse can otherwise
skip rebuilding even when the archive no longer exists.

Invalidate matching worker and shared caches. Keep affected apps paused until
their images have been rebuilt and their workloads redeployed. Leave workspace
files, source packages and checkpoints for their own storage cutovers.

Delete only the reviewed archive keys, including their versions, delete markers
and unfinished multipart uploads. This deletion is irreversible. Delete a source
bucket only when its entire contents and every consumer belong to this cutover;
remove its Terraform resource and IAM bindings in the same change. Verify the
named objects are gone and retained resources are intact.

## Start and verify

Deploy the chart with descriptor version 3 and the new bucket credentials. Remove
legacy archive bucket, prefix and signing-endpoint overrides. Resume services
against the empty R2 archive store, then rebuild and redeploy the owner's workloads.

Prove image upload, wrong-checksum rejection, signed download, multipart cleanup
and a cold worker image pull followed by workload execution. Verify retention on
a disposable archive. The reset
has no data rollback; an application rollback cannot restore deleted objects.
