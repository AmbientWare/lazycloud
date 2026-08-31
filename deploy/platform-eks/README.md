# Platform EKS

This Terraform module owns the hosted AWS deployment: VPC, EKS, managed Redis,
IAM, ECR, S3, Secrets Manager entries, the PlanetScale branch, and Argo CD.
Customer accounts remain behind the connected-AWS CloudFormation boundary
because the platform holds no credentials for them.

See `LIFECYCLE.md` for creation and teardown. The Helm workloads live in
`deploy/chart`; Argo CD applies them from the deployment branch.

## Private network

The chart runs a two-replica WireGuard gateway behind a UDP `LoadBalancer`
Service. Set `wireguard_public_endpoint` to the stable `<host>:<port>` agents can
reach. The host can use Route 53, Cloudflare DNS, or another DNS provider. It
must resolve to a service that carries UDP to the gateway; a Cloudflare HTTP
tunnel does not carry WireGuard traffic.

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

## Ownership rules

`provider_aws.connection_policy` owns the connected-account permission set.
Terraform consumes its rendered policy; do not maintain another copy here.

`control_role_name` is a durable external contract after a customer connects.
Customer trust policies name its ARN, and recreating the same IAM name does not
restore the old role identity.

Every global AWS name carries `var.deployment` except that control role. Two
deployments in one account need distinct deployment names, control role names,
WireGuard public endpoints, Cloudflare tunnels and hostnames, and Stripe test or
live configuration.

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
