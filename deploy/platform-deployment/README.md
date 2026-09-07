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

[R2 image archives](IMAGE_ARCHIVES.md) describes credentials, the resumable
copy command, and the required offline transition to infrastructure descriptor
version 3. Do not deploy the new archive settings before verified coordinate
cutover. Existing S3 buckets remain intact.

[Provider provisioning](../PROVIDERS.md) covers worker capacity. `capacity.tf`
declares the verified image input; Helm owns the Ashburn warm default and node
sizes. Supply the
`hetzner-images.tfvars.json` image-workflow artifact to Terraform and add
`LAZYCLOUD_PLATFORM_CAPACITY_HETZNER_TOKENS` to the existing operator secret.
Only credentials are operator-owned; capacity policy is deployment-owned.

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
cluster's, a Helm `runtime.LAZYCLOUD_WIREGUARD_PUBLIC_ENDPOINT`, a `deploy/cloudflare` apply of its own
with `cloudflare_state_key` naming it, and its own `github_environment`. What it
shares: the cluster, the images, the storage class, the External Secrets
operator, and Argo.

## Private network

The chart runs a two-replica WireGuard gateway behind a UDP `LoadBalancer`
Service, one per deployment. Set Helm `runtime.LAZYCLOUD_WIREGUARD_PUBLIC_ENDPOINT` to the stable
`<host>:<port>` agents can reach. The host can use Route 53, Cloudflare DNS, or
another DNS provider. It must resolve to a service that carries UDP to the
gateway; a Cloudflare HTTP tunnel does not carry WireGuard traffic.

The gateway replicas share one server keypair. Redis grants one replica the
active lease while the other is ready to take over. The pair provides failover,
not twice the packet throughput.

Control-plane replicas run as a StatefulSet with one stable WireGuard keypair
per ordinal. Keep `controlPlane.replicas` and `wireguard.platformPeers` equal.
Agents generate and retain their own private keys. Postgres stores agent public
keys, assigned addresses, revocation state, and handshake observations.

## WireGuard key storage

Terraform declares one `<deployment>/wireguard` Secrets Manager entry. It does
not put key material in Terraform state. The chart's `wireguard-bootstrap` Job
generates the gateway pair and the configured platform pairs, then writes one
JSON document through a narrowly scoped Pod Identity role. Repeated runs reuse
the complete document.

External Secrets projects the keys into the gateway and platform containers as
read-only files. Secrets Manager is read during bootstrap and projection, not
for enrollment or packet forwarding. One document per deployment is enough;
there is no secret per agent.

Do not edit or delete that document on a persistent installation. Replacing the
server key changes the gateway identity and invalidates every enrolled peer
configuration.

## Secrets and identity

The External Secrets operator is installed once, by Argo, into its own
namespace, and holds no AWS identity. Each deployment's chart declares a
`SecretStore` that presents the `secrets-reader` service account in its
namespace; the operator exchanges that account's token for this module's
`<deployment>-secrets-reader` role, whose trust names exactly that namespace
and whose policy reads exactly `<deployment>/*`. Staging cannot read prod's
documents because IAM refuses the subject, not because a chart is careful.

Every other identity is a Pod Identity association in this namespace: the
control plane and scheduler hold `<deployment>-control-plane`, the bootstrap
Job holds `<deployment>-wireguard-bootstrap`.

## Ownership rules

`provider_aws.connection_policy` owns the connected-account permission set.
Terraform consumes its rendered policy; do not maintain another copy here.

`control_role_name` is a durable external contract after a customer connects.
Customer trust policies name its ARN, and recreating the same IAM name does not
restore the old role identity. Unset, it is `<deployment>-control-principal`.

Operator-supplied credentials belong in `<deployment>/operator`. Terraform owns
`<deployment>/platform`, and the WireGuard bootstrap owns
`<deployment>/wireguard`. Each document has one writer so an apply cannot erase
values supplied through another path.

## Scaling

The API replicas and their platform peers scale together. Gateway replicas are
active and standby because one server identity owns the endpoint. If one gateway
reaches its measured packet or peer limit, the next scaling boundary is another
gateway endpoint and peer shard, not more active replicas sharing the same
endpoint. Sharding is not implemented by this module yet.

## Application handoff

Terraform writes `configuration/infrastructure-v1.json` into the deployment
bucket. Set its `infrastructure_config_uri` output as the GitHub environment
variable `INFRASTRUCTURE_CONFIG_URI`. Deploy reads only this non-secret document.
It cannot read Terraform state or write a release pointer.

Prices, Stripe account selection, fleet limits and credential property bindings
live in Helm. Change them in Git and deploy without applying Terraform.
The database server ceiling remains `database_max_connections` here and is
exported to Helm for pool budgeting. See [the transition checklist](../CONFIGURATION.md).
