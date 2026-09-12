# Platform deployment

One deployment of the platform: `lazycloud-prod`, and later `lazycloud-staging`
beside it on the same cluster. This module owns what a deployment cannot share:
the managed Redis, the S3 buckets, the Secrets Manager documents, the
PlanetScale branch, the fleet network and connection role, the Cloudflare
tunnel it reads, and every AWS identity its workloads hold. The cluster, the
image repositories and Argo are `deploy/platform-core`, applied once and read
here through its state.

See `LIFECYCLE.md` for creation and teardown. The Helm workloads live in
`deploy/chart`; Argo CD applies them into the namespace named for the
deployment, from the branch named for it, and the values file Deploy writes
there.

[Object storage](OBJECT_STORAGE.md) describes workload identity, scoped workspace
access and the application data cutover. Infrastructure descriptor version 7 names
the S3 endpoint, bucket identities, workspace grant role and regional fleet networks.

Use the shared [S3 Terraform backend](../terraform-state/README.md):

```sh
export TF_VAR_terraform_backend_config="$HOME/.lazycloud/operator/terraform-backend.json"
terraform init -backend-config="$TF_VAR_terraform_backend_config" \
  -backend-config="key=platform-deployment/lazycloud-prod.tfstate"
```

[Provider provisioning](../PROVIDERS.md) covers worker capacity. `capacity.tf`
declares the verified image input; Helm owns the Ashburn warm default and node
sizes. Supply the
`hetzner-images.tfvars.json` image-workflow artifact to Terraform and add
`LAZYCLOUD_PLATFORM_CAPACITY_HETZNER_TOKENS` to the existing operator secret.
Only credentials are operator-owned; capacity policy is deployment-owned.

AWS fleet workers can launch in `us-east-1` and `us-west-2`. The deployment
owns a VPC in each region and registers both through `fleet.networks`. The
connection role and node identity are shared across regions. Adding a region
or subnets preserves every existing network; removing or replacing one is
rejected while registering the fleet.

For this rollout, apply and publish the version-7 infrastructure descriptor,
publish CPU and GPU images through Connected AWS Node Images, then publish a
host release containing both regional image catalogs. Deploy the application
with that host release pinned. Migration `0033_aws_regional_networks` preserves
existing connection networks before fleet registration adds West. Existing
customer-managed authorization stacks retain their own region.

Image baking selects tagged public fleet subnets with an active internet route
and their fleet security group. Apply the release-assets stack's EC2 inventory
permissions before running the image workflow. It does not rely on default VPCs.

Set `region="us-west"` in SDK workload configuration to require Oregon.
Automatic placement may choose either approved region. A region needs its
network, image, eligible offer and supplier quote before it can supply capacity.
Customer rates and the selected-location multiplier remain unchanged. Storage
stays in its existing region; cross-region transfer remains a supplier expense.

PostgreSQL application traffic uses the branch's built-in PgBouncer on port 6432.
Terraform also publishes an explicit port-5432 URL for migrations, administration
and session advisory locks. `database_pooler_max_connections` bounds pooled
backends separately from application client pools. Follow the first-transition
steps in [deployment configuration](../CONFIGURATION.md) before switching an
existing direct-only deployment.

## Two deployments, one cluster

Each deployment is a namespace, `lazycloud-prod` or `lazycloud-staging`, and
`var.deployment` is both the namespace and the prefix of every AWS name. The
GitHub environment of the same short name holds the deploy role's ARN, and the
role's trust admits that environment only.

What a second deployment needs distinct: its own `deployment`, a Stripe test
account and key, a `fleet_cidr` that overlaps neither the other's nor the
cluster's, its own agent tunnel hostname and CA, a `deploy/cloudflare` apply of its own
with `cloudflare_state_key` naming it, and its own `github_environment`. What it
shares: the cluster, the images, the storage class, the External Secrets
operator, and Argo.

## Agent connections

The chart owns a two-replica connection gateway Deployment and one public TCP443
NLB. Gateways terminate mutual TLS and reach the API Service at port9000. API
replicas have no networking sidecars or stable peer identities. The deployment's
operator secret holds the issuer and gateway bootstrap credential; only API pods
mount the issuer key. Follow [connection gateway deployment](../connection-gateway.md)
for initial CA bootstrap, the dedicated DNS record, and the clean cutover.

## Secrets and identity

The External Secrets operator is installed once, by Argo, into its own
namespace, and holds no AWS identity. Each deployment's chart declares a
`SecretStore` that presents the `secrets-reader` service account in its
namespace; the operator exchanges that account's token for this module's
`<deployment>-secrets-reader` role, whose trust names exactly that namespace
and whose policy reads exactly `<deployment>/*`. Staging cannot read prod's
documents because IAM refuses the subject, not because a chart is careful.

The control plane and scheduler hold `<deployment>-control-plane` through Pod
Identity. Connection gateway pods have no AWS identity.

## Ownership rules

`provider_aws.connection_policy` owns the connected-account permission set.
Terraform consumes its rendered policy; do not maintain another copy here.

`control_role_name` is a durable external contract after a customer connects.
Customer trust policies name its ARN, and recreating the same IAM name does not
restore the old role identity. Unset, it is `<deployment>-control-principal`.

Operator-supplied credentials belong in `<deployment>/operator`. Terraform owns
`<deployment>/platform`. The operator document also owns the tunnel issuer and
gateway bootstrap credential. Terraform never writes those values.

## Scaling

API and gateway replica counts scale independently. `connectionGateway.replicas`
adds gateway pods behind the existing NLB. Every pod generates its own key and
obtains a certificate through the API. Measure stream concurrency, memory, and
reconnect bursts before raising the count.

## Application handoff

Terraform exports `infrastructure_configuration`. Publish that JSON with
`python -m deploy.object_storage publish` to the `infrastructure_config_uri` output and set
that URI as the GitHub environment variable `INFRASTRUCTURE_CONFIG_URI`.
Deploy downloads this descriptor from S3 with its OIDC role and records its snapshot in Git.

Prices, Stripe account selection, fleet limits and credential property bindings
live in Helm. Change them in Git and deploy without applying Terraform.
The database server ceiling remains `database_max_connections` here and is
exported to Helm for pool budgeting. See [the transition checklist](../CONFIGURATION.md).
