# Object storage

AWS S3 stores platform-owned data through the shared S3-compatible client.
The application bucket holds images, source packages, artifacts and checkpoints.
Each workspace has its own bucket for files and mounted volumes. Customers can
attach their own storage for BYO infrastructure.

## Configuration

Terraform exports the regional endpoint, signing settings, bucket identities
and `LAZYCLOUD_AWS_WORKSPACE_STORAGE_ROLE_ARN`. The chart selects the AWS
workspace issuer. Control-plane and scheduler pods use their existing Pod
Identity role to administer deployment-owned storage and assume the dedicated
workspace role. Their environments contain no object-store access key or secret.

The shared connection settings remain:

- `LAZYCLOUD_OBJECT_STORE_ENDPOINT_URL`
- `LAZYCLOUD_OBJECT_STORE_REGION_NAME`
- `LAZYCLOUD_OBJECT_STORE_FORCE_PATH_STYLE`
- `LAZYCLOUD_OBJECT_STORE_BUCKET`
- `LAZYCLOUD_OBJECT_STORE_WORKSPACE_BUCKET_PREFIX`

Workers receive 15-minute STS grants scoped to one workspace bucket. The mount
refresh loop renews them before expiration, including on workers outside AWS.
Grants cannot administer buckets or access another workspace. Retirement fences
outstanding grants before purging objects and incomplete uploads. Image
transfers use signed requests. Image archives share the application connection
and the `image-archives/` prefix.

Customer buckets require their own explicit credentials and endpoint. They never
inherit platform credentials. Local development uses the existing Garage
provider with its own administration credentials.

Application and workspace buckets are private. CORS permits dashboard requests
without granting object access. S3 gateway endpoints attach to cluster and fleet
route tables. Same-region S3 traffic uses those routes; public downloads,
cross-region traffic and off-AWS workers can still incur transfer charges.

## Migration and deployment

Preserve deployed data. Terraform removal blocks retain old R2 buckets and their
configuration outside Terraform state. They do not copy data or authorize
deletion. Record exact resource identities before applying and retain the
inventory until migration and scoped retirement are verified.

1. Review normal core and deployment plans. They must add S3 resources and
   endpoints without replacing networks, durable roles or databases, or deleting
   R2 data.
2. Provision S3 and verify workload permissions. Keep the installation on its
   recorded descriptor until the coordinated cutover.
3. Stop admission and all writers, including cleanup, builds and mounted volumes.
   Account for outstanding signed uploads and storage grants.
4. Copy the application bucket and each managed workspace bucket. Preserve keys,
   metadata and content; verify counts and content checksums. Do not copy or
   change customer-owned storage.
5. Use the deployment migration owner to update persisted managed workspace
   coordinates and application bucket references together. Clear affected mount
   and cache state. Retain source data and the pre-cutover database record until
   acceptance passes.
6. Publish the version-5 infrastructure descriptor and deploy its matching chart
   and application code. Verify production workflows before reopening admission.

Release publication and Terraform backends have a separate migration. Preserve
immutable release URLs, all state keys and locking. Use Terraform's backend
migration; never start with an empty state or discard an old state object.

Run the application migration with `uv run python -m deploy.migrate_object_storage
--configuration /private/migration.json --infrastructure /private/infrastructure.json`.
The configuration contains the old descriptor's `source` object-store section,
`source_profile`, `cloudflare_account_id`, `token_owner` as `account` or `user`,
the deployed `writer_token_id`, `kubernetes_context`, `namespace`,
`argocd_namespace` and `argocd_application`. Copy the writer token ID from the
deployment's actual object-store access-key ID. Do not substitute another token.
The database connection uses the canonical `LAZYCLOUD_DATABASE_URL` setting;
the target uses the operator's AWS default identity. Neither input file contains
storage secrets.

The source profile must hold a separate permanent R2 read-only token. The existing
`CLOUDFLARE_API_TOKEN` must be able to inspect both token records through their
account or user token API. A deleted token's 404 is insufficient proof; disable
the exact deployed parent token and keep its record until migration completes.
The command checks its disabled or expired status and the reader's read-only
permissions. Cloudflare exposes no account-wide proof that every possible writer
has stopped. Before execution, inventory other user tokens, Workers bucket
bindings and external clients; revoke their write access or remove their source
bindings. If that inventory is incomplete, the source is not frozen and cutover
must not proceed.

Drain tasks, builds and mounts, suspend cron jobs, disable Argo automatic sync,
finish any Argo operation and scale application writers to zero. The execution
checks these Kubernetes records and requires no live pods, active database work,
incomplete multipart uploads, write claims or pending object/archive cleanup.
Old grants and signed URLs derive from the disabled storage parent token. Keep
publishers and workers stopped until cutover acceptance passes.

Review the printed bucket map, then repeat with `--execute`. The command holds
PostgreSQL write locks for the affected tables through copy and atomic coordinate
updates. It creates only managed workspace destination buckets, verifies AWS
bucket ownership, preserves object HTTP headers, and downloads both sides to
compare SHA-256. Conflicting destination objects stop the run. Source inventory
and headers are checked again before commit. Application-purpose keys stay the
same; keys containing the old physical application bucket are relocated with
their owning object records. Volume sizes, metering times and billing history are
unchanged. Customer bucket credentials and endpoints remain untouched.

A failure leaves source objects and database coordinates intact; copied target
objects can be verified on the next run. Do not resume old writers after a
successful coordinate commit. Keep the database backup and R2 source data until
the new installation passes acceptance. Old worker caches and mounts must be
discarded through worker retirement before they can rejoin the installation.

## Release hostname cutover

The deployment owns a private S3 release bucket and a CloudFront distribution
with origin access control. ACM validates its certificate in us-east-1 through
the existing Cloudflare zone. The public hostname and every object path remain
unchanged, so published manifests and old worker bootstrap URLs remain valid.

1. Migrate Terraform backends as described in the state runbook. Apply the
   Cloudflare root's reviewed removal blocks and zone-id output. They retain the
   R2 bucket and its active custom-domain binding.
2. Prepare the new release bucket, certificate, distribution and bucket policy.
   For this migration only, review a targeted deployment plan for
   `aws_cloudfront_distribution.releases` and `aws_s3_bucket_policy.releases`.
   Do not apply `cloudflare_dns_record.releases` while R2 owns that hostname.
3. Pause release publishers. Copy every release object and metadata, then compare
   content hashes. Verify the distribution directly before changing DNS.
4. Remove only the inventoried R2 custom-domain binding, retaining the bucket and
   data. Apply the reviewed deployment plan to create the same hostname's CNAME.
   This binding transition is a cutover window; do not claim continuous service
   while DNS caches and the old binding are changing.
5. Update the existing release CloudFormation stack with the new `ReleaseBucketName`
   parameter, preserving all existing publisher identity parameters and its role
   identity. Update GitHub's bucket and regional endpoint variables. Its OIDC role
   supplies credentials; remove the old object-store secret bindings.
6. Download retained manifests and every referenced binary/template anonymously,
   publish and verify a new release, then resume publishers. Review a normal
   Terraform plan after any targeted apply. Retain R2 until these checks pass.

Retire an R2 bucket and its credentials only after all readers and writers have
switched and data verification passes. Inventory exact deletion targets and
handle retirement separately. Cloudflare DNS and ingress remain.

Acceptance covers SDK uploads/downloads, mounted writes and credential refresh,
cross-workspace denial, image builds and cold-worker execution, checksums,
multipart cleanup, release downloads and Terraform locking.
