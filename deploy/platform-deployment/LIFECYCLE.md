# Create or retire a hosted deployment

Use this guide for a new hosted installation or an explicitly authorized teardown.
For routine releases, use the [runbook](../RUNBOOK.md#shipping-from-actions).
Run commands from the repository root.

## Identify the owners

| Owner | Resources |
| --- | --- |
| `deploy/platform-core` | Shared cluster, VPC, registries, storage class, and Argo |
| `deploy/platform-deployment` | One deployment's database, Redis, buckets, network, secret documents, and IAM roles |
| Argo CD | Kubernetes workloads recorded on the deployment branch |
| Compute service and scheduler | Runtime provider capacity and its cleanup |

Retire runtime capacity while the control plane still runs. Terraform does
not own the machines and groups created dynamically by the compute service.

## Prepare credentials and state

Use AWS profile `default` for platform infrastructure. Keep operator credentials
in a private directory outside the checkout. You need the Cloudflare operator
token, PlanetScale service token and token ID, Stripe operator key, and the
organization's GitHub App private key.

Create the [S3 state backend](../terraform-state/README.md) before initializing
the modules. Existing installations must preserve their current state.

```sh
export AWS_PROFILE=default
export TF_VAR_terraform_backend_config="$HOME/.lazycloud/operator/terraform-backend.json"
```

Supply credentials from the secret manager or a protected environment file.
Do not put credential values into shell command history, Git, or plan output
shared with others. Argo needs the GitHub App key at cluster bootstrap to read
the repository before External Secrets is available.

## Create the cluster once

Skip this step if the shared cluster already exists.

```sh
terraform -chdir=deploy/platform-core init \
  -backend-config="$TF_VAR_terraform_backend_config" \
  -backend-config="key=platform-core/lazycloud.tfstate"
terraform -chdir=deploy/platform-core plan -out=core.tfplan
```

Set `cluster_api_cidrs` to include the operator's address, then review the
plan before `terraform -chdir=deploy/platform-core apply core.tfplan`.
See [platform core](../platform-core/README.md) for bootstrap inputs and
partial-creation recovery.

## Prepare the deployment

Read [provider provisioning](../PROVIDERS.md) for required host images and
credentials. Configure HTTP ingress through [Cloudflare](../cloudflare/README.md)
with a distinct state key when adding another deployment.

Initialize the deployment's state:

```sh
DEPLOYMENT=lazycloud-prod
terraform -chdir=deploy/platform-deployment init \
  -backend-config="$TF_VAR_terraform_backend_config" \
  -backend-config="key=platform-deployment/$DEPLOYMENT.tfstate"
terraform -chdir=deploy/platform-deployment plan \
  -var-file=/absolute/path/to/deployment.tfvars \
  -var-file=/absolute/path/to/hetzner-images.tfvars.json \
  -out=deployment.tfplan
```

Your private variables must name the deployment, GitHub environment, database
organization, and non-overlapping networks. Check all replacements and deletions.
Apply only the reviewed plan:

```sh
terraform -chdir=deploy/platform-deployment apply deployment.tfplan
```

Provider definitions own approved shapes, locations, and supplier prices.
`compute.fleet_policy` owns warm targets and capacity limits. Helm owns
application settings and secret bindings.

## Populate operator secrets

Terraform owns `<deployment>/platform`. Operators own the values in
`<deployment>/operator`. The chart's `secrets.map` lists required properties.

Populate the operator document before the first sync, including the administrator
token and service credentials. For an existing document, read its current version,
merge only the intended properties, and preserve every unrelated value.
Do not replace it with an example JSON object.

Configure the administrator token before the bootstrap Job runs. Recover a lost
administrator credential through the account recovery procedure; do not reset a
persistent database.

Initialize the tunnel issuer and gateway bootstrap credential with the
[connection gateway procedure](../connection-gateway.md#bootstrap-credentials-once).
That command preserves existing properties and checks for concurrent changes.

## Configure GitHub and publish the descriptor

Each deployment has a GitHub environment, such as `prod` or `staging`.
Set its deployment role and infrastructure descriptor location:

```sh
gh secret set AWS_DEPLOY_ROLE_ARN --env prod \
  --body "$(terraform -chdir=deploy/platform-deployment output -raw deploy_role_arn)"
gh variable set INFRASTRUCTURE_CONFIG_URI --env prod \
  --body "$(terraform -chdir=deploy/platform-deployment output -raw infrastructure_config_uri)"
```

Publish the non-secret `infrastructure_configuration` output to that S3 URI
using `uv run --group workspace python -m deploy.object_storage publish`.
The [release-assets guide](../aws-release-assets/README.md) has the exact command
and repository variables. GitHub uses OIDC; do not upload AWS access keys.

Required reviewers on the production environment control promotion approval.

## Publish and deploy the first release

Publish a recipe-compatible node-image catalog before the first Ship.
Then dispatch Ship from `main`:

```sh
gh workflow run ship.yml --ref main -f bump=patch
```

Ship publishes client packages and a complete immutable release manifest, then
records the selected release for Argo. For an existing release, Deploy renders
configuration from its manifest without rebuilding artifacts.

Argo runs secret projection, schema migrations, administrator and billing
bootstrap, workloads, and fleet registration in their declared sync waves.
Check several signals together:

```sh
aws eks update-kubeconfig --name lazycloud --region us-east-1
kubectl -n argocd get applications
kubectl -n lazycloud-prod get pods
kubectl -n lazycloud-prod get jobs
kubectl -n lazycloud-prod describe secretstore aws-secrets-manager
```

Investigate failed Jobs or stalled pods before another sync. Configure the
gateway's assigned NLB hostname through the DNS owner, then verify an
authenticated agent can execute a real workload. Pod readiness alone does
not establish a working deployment.

## Add staging

Repeat the deployment steps with its own namespace, state key, database,
Redis, buckets, operator secrets, Stripe test account, networks, tunnel hostname,
CA, and GitHub environment. Reuse the shared cluster and image registries.

Create the staging deployment branch before adding its Argo Application under
`deploy/argocd/apps` on `main`. Set the Application to read that branch into
the staging namespace. Promotion selects staging's complete release and renders
production's own infrastructure values.

## Retire a deployment

This deletes persistent infrastructure and data. Before proceeding, obtain
authorization for the exact deployment, inventory its resources, and verify
backups of data and credentials the owner needs to retain.

1. Stop new work and drain existing workloads.
2. Use `lazycloud-admin fleet destroy` against that deployment while its
   control plane and provider credentials remain available.
3. Confirm the command succeeded and the provider reports the owned capacity
   removed. Investigate lease contention or incomplete deletion before continuing.
4. Remove the deployment's Argo Application through Git and verify its workloads
   stop. The Application finalizer can delete its managed resources.
5. Review a Terraform destroy plan for that exact state and variables. Confirm
   every deletion belongs to the authorized deployment before applying it.
6. Inventory runtime-created workspace buckets, detached disks, retained images,
   and secret recovery windows separately. Terraform completion does not prove
   all billable resources are gone.

PlanetScale can refuse to delete a branch role while it owns schema objects.
Only during the approved full database teardown, preserve a private state backup
and remove that exact role's Terraform address so branch deletion can remove it:

```sh
terraform -chdir=deploy/platform-deployment state rm planetscale_postgres_branch_role.control_plane
```

Verify the role and branch identities first. This is not a repair procedure
for a running database.

The shared cluster remains until every deployment has been retired. Follow
[platform-core teardown](../platform-core/README.md#taking-it-down) only then.
Never reset production data as part of an update or recovery.
