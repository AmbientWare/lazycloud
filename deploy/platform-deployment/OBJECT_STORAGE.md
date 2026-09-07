# Object storage

R2 stores platform-owned object data through the shared S3-compatible client.
Customers can attach their own storage for BYO infrastructure.
The application bucket holds images, source
packages, artifacts and checkpoints. Each workspace has its own bucket for
files and mounted volumes. Deployment descriptors, public releases and Terraform
state have separate buckets because their readers and lifetimes differ.

## Configuration

The platform uses one S3-compatible storage configuration:

- `LAZYCLOUD_OBJECT_STORE_ENDPOINT_URL`
- `LAZYCLOUD_OBJECT_STORE_REGION_NAME`
- `LAZYCLOUD_OBJECT_STORE_FORCE_PATH_STYLE`
- `LAZYCLOUD_OBJECT_STORE_ACCESS_KEY_ID`
- `LAZYCLOUD_OBJECT_STORE_SECRET_ACCESS_KEY`
- `LAZYCLOUD_OBJECT_STORE_BUCKET`
- `LAZYCLOUD_OBJECT_STORE_WORKSPACE_BUCKET_PREFIX`

Terraform exports the endpoint, signing settings and bucket identities. Store the credential pair
once in the deployment's operator secret document. The R2 Admin Read & Write
credential permits the trusted platform to create workspace buckets and manage
their objects. Only trusted storage consumers receive it. The Cloudflare provider
issues temporary workspace credentials; the shared transport handles object I/O.

The local `lazycloud-object-storage` credential profile holds the same pair for
operator publication and Terraform. AWS infrastructure commands use `default`.
Cloudflare's provisioning API token and the application's custom-hostname token
have separate permissions and remain separate credentials.

Workers using platform storage receive short-lived object credentials scoped to one workspace bucket.
They cannot administer buckets or access another workspace. Image transfers use
signed requests. Archive storage inherits the primary connection and uses the
`image-archives/` prefix; it has no separate backend settings or secret aliases.

Customer-owned buckets use the customer's storage credentials and endpoint.
Attaching an external bucket never grants it the platform credential pair.
Keep storage contracts and configuration names provider-neutral; provider
packages own vendor-specific administration and credential mechanisms.

Application and workspace buckets are private. Their CORS rules allow dashboard
transfers without granting object access. Public release objects use the R2
custom domain. Signed private transfers use `r2.cloudflarestorage.com`.

## Reset and deployment

The owner authorized discarding the installation's existing object data. This
deletion is irreversible. Inventory exact buckets, versions, incomplete uploads
and database references before acting. Preserve unrelated resources.

Provision and verify R2 first. Stop admission and writers, drain tasks and
mounted volumes, and account for outstanding signed requests and credentials.
Reset image/build, source, checkpoint and workspace storage references together
through their owners. Invalidate affected worker and shared caches, then deploy
the R2 configuration and rebuild workloads.

Republish release artifacts and deployment descriptors from source. Transfer
Terraform's current resource ownership to its private R2 backend before deleting
the AWS state bucket. Preserve resource IDs and verify every state key, including
retired roots that still own infrastructure.

Remove old AWS buckets, their versions and multipart uploads only after every
consumer has switched. Remove their IAM policies, configuration and credentials
in the same cutover. Verify the named buckets are absent and retained compute,
databases, networking and registries remain intact.

Acceptance covers SDK uploads/downloads, mounted workspace writes and credential
refresh, cross-workspace denial, image builds and cold-worker execution,
checksums, multipart cleanup, release downloads and Terraform locking.
