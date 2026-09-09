# Terraform state on S3

All four Terraform roots use one private AWS S3 bucket. Each root retains its
own key and a `.tflock` object. State contains credentials and must never be
printed, committed, published or accessible to application and deployment roles.

The operator owns the state bucket separately from application infrastructure.
Before initializing a new installation, create an account-qualified bucket in
the chosen AWS region, block all public access, enforce bucket-owner ownership,
enable encryption and versioning, and verify those settings. Do not include the
bucket in application teardown.

Copy [backend.example.json](backend.example.json) to a private operator directory.
Set the actual bucket and region. Use the `default` operator profile; do not put
credentials in backend JSON, because Terraform copies it into local metadata
and saved plans.

```sh
export AWS_PROFILE=default
export TF_VAR_terraform_backend_config="$HOME/.lazycloud/operator/terraform-backend.json"
```

The state identity needs bucket listing and object reads/writes, including
deleting lock objects. Keep `encrypt` and `use_lockfile` enabled. The deployment
CI role reads its infrastructure descriptor and has no state-bucket permissions.

## Initialize a new installation

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

Use a different deployment key per deployment and Stripe key per Stripe account.
These roots do not use Terraform workspaces.

## Migrate existing R2 state

Stop all concurrent Terraform operations. Inventory every source state key,
including retired roots that still own resources. Keep the source backend JSON
and its `lazycloud-object-storage` credential profile available for migration.
Back up state privately and verify the current resource addresses and IDs.

Start from each root's initialized directory, whose cached backend identifies
the source. If that metadata is absent, initialize against the verified source
backend first. Then point the operator backend JSON at the verified destination
S3 bucket and migrate each root without changing its key:

```sh
terraform -chdir=deploy/platform-core init -migrate-state \
  -backend-config="$TF_VAR_terraform_backend_config" \
  -backend-config="key=platform-core/lazycloud.tfstate"
```

Review Terraform's migration prompt. Do not use `-reconfigure`, empty state,
imports or resource recreation as substitutes for transferring ownership.
Repeat for Cloudflare, every deployment and Stripe.

Compare source and destination resource addresses, IDs, serials and lineage
using private files. Terraform may change lineage during migration; record the
destination. Verify locking and run a normal plan for every root. The deployment
root must read the migrated core and Cloudflare outputs successfully. An
unexpected replacement or deletion is a blocker to applying.

Keep the source objects and versions until all keys are verified. Retiring the
old backend and its credential profile is a separate scoped operation. A local
`.terraform/terraform.tfstate` records backend configuration; zero resources in
that file say nothing about the remote state.

See [Terraform's S3 backend and locking contract](https://developer.hashicorp.com/terraform/language/backend/s3)
and [state migration behavior](https://developer.hashicorp.com/terraform/cli/commands/init#backend-initialization).
