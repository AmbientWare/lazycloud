# Terraform roots

| Root | State key | Owns |
| --- | --- | --- |
| platform-core | `platform-core/lazycloud.tfstate` | VPC, EKS Auto Mode cluster, node capacity, Argo CD and its root Application, OIDC provider, reference image repositories, workload image creation template |
| platform-deployment | `platform-deployment/<deployment>.tfstate` | One deployment: database, buckets, secret documents, fleet networks and node identity, workload roles, host connection certificate, chart values |
| cloudflare | `cloudflare/production.tfstate` | Tunnel and its credentials, platform DNS records |
| stripe | one per Stripe account | Webhook endpoint and events |
| images | `images/lazycloud.tfstate` | Release image repositories and role, GitHub release environment, tag rule and OIDC subject template |

Apply order for a new installation: platform-core, cloudflare,
platform-deployment, images, stripe; then cloudflare again with the load
balancer hostnames. Plans run from an operator machine with the `default`
profile after `aws sts get-caller-identity` names the platform account.
GitHub Actions never applies.

## State

Every root uses one private S3 bucket, its own key and a `.tflock` object.
State contains credentials and must never be printed, committed or readable
by application and deployment roles. Before the first init, create an
account-qualified bucket in the platform region with public access blocked,
bucket-owner ownership enforced, encryption and versioning on; it is not part
of any teardown.

Copy [backend.example.json](backend.example.json) to a private operator
directory and set the bucket and region. Keep `profile` at `default` and
`encrypt` and `use_lockfile` on; never put credentials in it, because
Terraform copies it into local metadata and saved plans.

```sh
export AWS_PROFILE=default
export TF_VAR_terraform_backend_config="$HOME/.lazycloud/operator/terraform-backend.json"
terraform -chdir=deploy/terraform/<root> init \
  -backend-config="$TF_VAR_terraform_backend_config" -backend-config="key=<state key>"
```

The roots moved from `deploy/<root>` on main to `deploy/terraform/<root>`
with their state keys unchanged; an initialized working directory carries
over with `init` in the new path.

`deploy/check.sh` runs `fmt -check`, `init -backend=false` with the
committed lock files and `validate` on every root. Nothing there reaches AWS.
