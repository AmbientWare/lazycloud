# R2 image archive rollout

Image archives use a private R2 bucket. Workers upload and download through
short-lived signed S3 URLs. Terraform publishes the bucket and endpoint in
infrastructure descriptor version 3; the chart supplies a separate archive
backend to the control plane and scheduler.

The initial transition is a clean reset of the owner's application storage.
Existing objects will not be copied or retained for recovery. This code provisions
the archive destination; it does not delete existing buckets or reset application
records. The coordinated storage reset must be implemented and reviewed before
deploying these settings to the existing installation.

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

## Coordinate the storage reset

The reset also needs the R2 workspace bucket and credential lifecycle. Provision
and validate both destinations before removing source data.

Inventory the exact deployment and workspace buckets, object versions, multipart
uploads and consumers. Build the deletion list from that inventory. Application
storage may share a bucket with artifacts still needed by deployment or running
workers. Terraform state, release artifacts and active deployment configuration
are outside the application-data reset; resolve any shared bucket before deleting
it. Remove obsolete Terraform resources and IAM bindings with their consumers so
a later apply cannot recreate the retired buckets.

Stop workload admission, image builds, checkpoint publication and retention.
Drain running work and storage mounts, suspend reconciliation that would restart
old services, and stop the control plane and scheduler. Confirm issued upload
and storage credentials have expired and in-flight transfers have ended. Poll
worker, database and object-store signals; stopping an API does not revoke a
previously issued capability.

Reset the affected durable image, checkpoint, source and workspace storage
references through their owning services. Invalidate the corresponding worker
and shared caches. Preserve accounts and billing history. Workloads must rebuild
or redeploy from available source rather than refer to deleted archives or files.
Deleting objects alone leaves the application claiming those objects still exist.

After those references and writers are resolved, empty and delete only the
reviewed source buckets, including versions, delete markers and unfinished
multipart uploads. This deletion is irreversible. Verify each named bucket is
absent and every retained resource is intact before reopening admission.

## Start and verify

Deploy the chart with descriptor version 3 and the new bucket credentials. Remove
legacy archive bucket, prefix and signing-endpoint overrides. Resume services
against the empty R2 stores, then rebuild and redeploy the owner's workloads.

Prove image upload, wrong-checksum rejection, signed download, multipart cleanup
and a cold worker image pull followed by workload execution. Verify retention on
a disposable archive and workspace credential isolation separately. The reset
has no data rollback; an application rollback cannot restore deleted objects.
