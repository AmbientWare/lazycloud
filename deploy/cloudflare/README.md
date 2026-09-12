# Cloudflare deployment

This module owns the public HTTP tunnel, its apex and wildcard DNS records, the
DNS-only agent tunnel record, and the configured SaaS fallback origin. It does
not own the zone, mail records, or customer records.

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

## Credentials and state

Use the shared private [S3 state backend](../terraform-state/README.md). Keep the
Cloudflare operator token outside Terraform variable files and workload secrets.
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

Read the existing zone records and review the plan before applying. During agent
tunnel cutover, the only DNS addition is the dedicated connection gateway record.
Apex, wildcard, mail, verification, custom-hostname and SaaS records must remain
unchanged. Apply only the reviewed plan, then verify both public HTTP and an
authenticated agent tunnel. Do not print tunnel credentials or operator tokens.
