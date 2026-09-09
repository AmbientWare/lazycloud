# Object storage

AWS S3 stores platform-owned data through the shared S3-compatible client.
The application bucket holds images, source packages, artifacts and checkpoints.
Each workspace has its own bucket for files and mounted volumes. Customer-owned
buckets keep their own credentials and endpoints.

Terraform exports `LAZYCLOUD_OBJECT_STORE_ENDPOINT_URL`,
`LAZYCLOUD_OBJECT_STORE_REGION_NAME`, `LAZYCLOUD_OBJECT_STORE_FORCE_PATH_STYLE`,
`LAZYCLOUD_OBJECT_STORE_BUCKET`, `LAZYCLOUD_OBJECT_STORE_WORKSPACE_BUCKET_PREFIX`
and `LAZYCLOUD_AWS_WORKSPACE_STORAGE_ROLE_ARN`. API and scheduler pods use Pod
Identity for platform storage. Workers receive renewable STS grants scoped to
one workspace bucket. Image transfers use signed requests. Local development
uses Garage.

Buckets are private. CORS permits dashboard requests without granting access.
S3 gateway endpoints attach to cluster and fleet route tables. Same-region
traffic uses those routes; public downloads, cross-region traffic and workers
outside AWS can still incur transfer charges.

## Hard cut to S3

Existing platform-owned R2 data will be deleted. There is no copy, migration
command or fallback storage path.

1. Provision the S3 resources and publish fresh release assets. Preserve the
   release hostname and update deployment pins to the new manifest.
2. Stop admission, drain work and delete old managed volumes through the normal
   cleanup owner. Retire old workers and stop application writers before changing
   storage credentials or applying the new schema. Old workers use removed
   usage-reporting routes and must be replaced.
3. Switch to the S3 descriptor and matching application release. Before reopening
   admission, manually clear references to discarded platform objects, images,
   source packages and checkpoints. Keep accounts, workspace identities, billing
   history, BYO storage and cloud connections.
4. Recreate workers, rebuild images and upload source again. Check a cold build,
   volume writes and artifact upload/download.
5. Empty the exact old R2 application, deployment, release and managed workspace
   buckets. Apply reviewed Terraform plans removing their former resources and
   bindings. Runtime-created workspace buckets are outside Terraform and must be
   deleted directly. If an earlier apply already removed a bucket from state,
   delete that bucket directly too.

Review the bucket inventory and Terraform destruction list before deletion.
Do not run `terraform destroy` against an entire platform root to remove storage.
Terraform state records infrastructure ownership; move its backend with
Terraform's normal state migration and preserve it. Unrelated Cloudflare DNS,
ingress and customer-owned buckets remain.
