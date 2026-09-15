# Operate object storage

AWS S3 holds platform-owned objects through the shared S3-compatible client.
Local development uses Garage. Customer-owned bucket mounts retain their own
endpoints and credentials.

## Configure a deployment

Terraform exports the endpoint, signing region, application bucket, workspace
bucket prefix, and workspace grant role in the infrastructure descriptor.
Publish that descriptor and select it through the normal
[deployment workflow](../CONFIGURATION.md#deploy-sequence).

API and scheduler pods use Pod Identity. Workers receive renewable STS grants
scoped to one workspace bucket. Do not distribute permanent platform keys to
workers.

The application bucket holds image archives, source packages, and checkpoints.
Workspace buckets hold workspace files, volumes, and artifacts. Buckets are
private; CORS permits browser requests but grants no object access.

## Check reads and writes

After a storage configuration change, run a cold image build, write a volume
file, and upload and download an artifact through the public SDK. Use
`examples/artifacts/app.py` with the target workspace selected. Inspect the
API, worker, and object-store errors if any step fails.

Signed downloads must be reachable from the browser as well as the workload.
S3 gateway endpoints serve same-region traffic from attached cluster and fleet
route tables. Cross-region requests and workers outside AWS may incur transfer
charges.

## Preserve stored data

Changing an endpoint or bucket does not move existing objects. Before a storage
migration, inventory object ownership and persisted references, verify backups,
and prepare a reviewed migration for that installation. Preserve customer-owned
buckets and all unrelated workspace data.

Delete files through the owning volume or artifact API. Runtime-created
workspace buckets are outside Terraform, so a Terraform destroy does not prove
their cleanup. Inventory exact bucket names before any authorized teardown.
Never destroy an entire platform root to remove a storage resource.

Terraform state has a separate [backend migration procedure](../terraform-state/README.md).
It is infrastructure ownership data and must be preserved.
