# Terraform state on R2

All four Terraform roots store state in one private R2 bucket. The `s3` backend
name identifies the protocol. State does not use the AWS S3 service. Each root
has its own key and a `.tflock` object for concurrent-writer exclusion.

The state bucket belongs to the operator. Create it before initializing any
root, keep public access disabled, and exclude it from application teardown.
State contains infrastructure credentials, including tunnel and webhook secrets.

## Shared operator configuration

Use the canonical trusted-operator credentials in the `lazycloud-object-storage` profile
in the operator's shared AWS credentials file. Other trusted R2 operator tools
use the same profile. Runtime workloads use their scoped credentials.

```ini
[lazycloud-object-storage]
aws_access_key_id = <operator-r2-access-key>
aws_secret_access_key = <operator-r2-secret-key>
```

Keep the credentials file private and preserve its existing AWS profiles. The
state backend selects `lazycloud-object-storage` explicitly; AWS infrastructure provisioning
continues to use `AWS_PROFILE=default`. Do not export the R2 pair as
`AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`, or set a global AWS S3 endpoint.

Copy [backend.example.json](backend.example.json) to a private operator directory.
Set its actual account endpoint and existing state bucket. Keep credentials out
of this JSON: Terraform records backend configuration in local metadata and
saved plans. Every root and both remote-state readers use this same file.

```sh
export AWS_PROFILE=default
export TF_VAR_terraform_backend_config="$HOME/.lazycloud/operator/terraform-backend.json"
```

The R2 principal needs bucket listing and object reads/writes, including deletion
of lock objects. `use_lockfile` must remain enabled. The compatibility switches
disable AWS-only discovery and checksum headers; TLS remains enabled. R2 encrypts
stored objects without Terraform requesting an AWS server-side encryption mode.

## Initialize a new state

```sh
terraform -chdir=deploy/platform-core init \
  -backend-config="$TF_VAR_terraform_backend_config" \
  -backend-config="key=platform-core/lazycloud.tfstate"
terraform -chdir=deploy/cloudflare init \
  -backend-config="$TF_VAR_terraform_backend_config" \
  -backend-config="key=cloudflare/production.tfstate"
terraform -chdir=deploy/platform-deployment init \
  -backend-config="$TF_VAR_terraform_backend_config" \
  -backend-config="key=platform-deployment/lazycloud-prod.tfstate"
terraform -chdir=deploy/stripe init \
  -backend-config="$TF_VAR_terraform_backend_config" \
  -backend-config="key=stripe/production.tfstate"
```

Use a different deployment key per deployment and a different Stripe key per
Stripe account. Terraform workspaces are not used for these roots.

## Transfer an existing state

Preserve Terraform's current resource ownership even when application data is
being discarded. An empty replacement state would orphan the cluster, networks,
identities and other live resources.

Stop concurrent Terraform operations. Start from each root's initialized working
directory, whose cached backend still identifies the existing state. Verify
`terraform state list` contains the expected resources. If that local backend
metadata is absent, initialize against the verified source backend first.

For each root, retain its exact current key and run:

```sh
terraform -chdir=deploy/platform-core init -migrate-state \
  -backend-config="$TF_VAR_terraform_backend_config" \
  -backend-config="key=platform-core/lazycloud.tfstate"
```

Terraform reads the current source state and writes it to R2. Review its migration
prompt. Do not use `-reconfigure`, an empty `state push`, or resource recreation
as a substitute for this ownership transfer. No resource import is needed when
the existing state is transferred intact.

Repeat for Cloudflare, each deployment, and Stripe. Compare source and destination
resource addresses, resource IDs and attributes using private temporary files.
Record the destination lineage; Terraform can create a new lineage during transfer.
Do not print full state or commit it. Verify each migrated root can read its R2
state and that concurrent operations reject the same held `.tflock`. The deployment
root must also read the migrated core and Cloudflare outputs successfully.

Run and review the first normal plan from R2 before retiring the old backend.
Treat source-state deletion as a separate, scoped operator action after all roots
have transferred and ownership matches. Migration does not require retaining old
state versions or application data.

Retired roots can still own resources. Include their state objects in the
ownership inventory even when their source directories are gone. Preserve those
objects under their existing keys until their resources are explicitly retired.
Do not infer an empty remote state from the zero resources in a local
`.terraform/terraform.tfstate` file: that file stores backend configuration.

Backend behavior follows [Cloudflare's R2 backend configuration](https://developers.cloudflare.com/terraform/advanced-topics/remote-backend/)
and [Terraform's S3 locking and credentials contract](https://developer.hashicorp.com/terraform/language/backend/s3).
