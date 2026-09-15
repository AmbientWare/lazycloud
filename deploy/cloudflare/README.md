# Configure Cloudflare ingress

Use the existing state and operator identity to update HTTP ingress or agent
tunnel DNS. Start with a saved plan; verify both routes after applying it.

This module owns the public HTTP tunnel, its apex and wildcard DNS records, the
DNS-only agent tunnel and TCP workload records, and the configured SaaS fallback
origin. It does not own the zone, mail records, or customer records.

The existing Terraform state owns the deployed HTTP tunnel and its credentials.
Preserve that state and every unrelated record. Never replace the tunnel or clear
the zone to add the agent endpoint.

## Agent tunnel DNS

After the chart creates the `connection-gateway` Service, read its provisioned
NLB hostname. Set `connection_gateway_endpoint` to that exact hostname in the
operator's Terraform variables. The module adds `tunnels.<public_hostname>` as a
DNS-only CNAME with a 60-second TTL. Its traffic remains TLS until it reaches a
connection gateway. The public HTTP tunnel cannot carry this authenticated TLS
connection. See [connection gateway deployment](../connection-gateway.md).

## Public TCP DNS

Set `tcp_ingress_endpoint` to the provisioned load balancer hostname for public
TCP ingress. Terraform owns one DNS-only CNAME, `*.tcp.<public_hostname>`, with a
60-second TTL. Every TCP workload hostname resolves through this record; creating
or deleting a workload does not change DNS. The ingress routes TLS connections
using SNI.

The record requires a reachable TCP listener and a trusted certificate covering
`*.tcp.<public_hostname>`. It does not provision either. The HTTP tunnel and its
edge certificate do not serve these direct TCP connections. Do not substitute
the HTTP tunnel or agent gateway hostname for the TCP load balancer.

The [application chart](../chart/README.md#public-tcp-ingress) owns the TCP Service
and wildcard Certificate. cert-manager owns temporary DNS validation TXT records;
Terraform owns the workload CNAME. Do not import the temporary TXT records into
Terraform. The certificate controller reuses the deploy Cloudflare token through
the operator secret document.

Use the existing operator Terraform credentials to manage this record. No DNS
controller or Cloudflare DNS token in the application is required. If the load
balancer is replaced, update `tcp_ingress_endpoint` and apply the reviewed plan.

## Credentials and state

Use the shared private [S3 state backend](../terraform-state/README.md). Keep the
Cloudflare operator token outside Terraform variable files and user workloads.
The operator token needs the permissions for the tunnel, DNS records, and any
configured SaaS fallback origin. Workload tokens retain their narrower permissions.

```sh
terraform -chdir=deploy/cloudflare init \
  -backend-config="$TF_VAR_terraform_backend_config" \
  -backend-config="key=cloudflare/production.tfstate"
terraform -chdir=deploy/cloudflare fmt -check
terraform -chdir=deploy/cloudflare validate
terraform -chdir=deploy/cloudflare plan -out=cloudflare.tfplan
```

Read the existing zone records and review the plan before applying. Adding TCP
ingress should add only its wildcard record. Preserve the HTTP apex and wildcard,
agent, mail, verification, custom-hostname and SaaS records. Apply only the reviewed
plan, then verify a public TCP workload through a normal TLS client with certificate
verification enabled. Do not print tunnel credentials or operator tokens.
