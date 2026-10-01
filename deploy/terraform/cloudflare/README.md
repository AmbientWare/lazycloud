# Cloudflare ingress

This root owns the public HTTP tunnel and its credentials, the proxied apex
and wildcard records pointing at it, the DNS-only `hosts.<apex>` and
`*.tcp.<apex>` records pointing at the chart's load balancers, the reference
platform's `tunnels.<apex>`, and optionally the Cloudflare for SaaS
fallback origin. It does not own the zone, mail records or customer records:
the zone carries live Google Workspace mail beside the flattened apex CNAME,
so never clear it and delete records by id.

```sh
terraform -chdir=deploy/terraform/cloudflare init \
  -backend-config="$TF_VAR_terraform_backend_config" \
  -backend-config="key=cloudflare/production.tfstate"
terraform -chdir=deploy/terraform/cloudflare plan -out=cloudflare.tfplan
```

Terraform reads `CLOUDFLARE_API_TOKEN` from the operator environment. The
same token, stored as `LAZYCLOUD_TCP_DNS_API_TOKEN` in the operator
document, lets cert-manager answer DNS-01 for `*.tcp.<apex>`.

## Load balancer records

After the chart creates `server-hosts` and `server-tcp`, read their
hostnames (`kubectl -n <deployment> get svc server-hosts server-tcp`) and set
`host_connection_endpoint` and `tcp_ingress_endpoint`. Review that the plan
changes only those records. Agents and TCP clients need both before they
connect; the apex and wildcard keep pointing at the tunnel throughout.

## Diagnosing HTTP 1033

A connector with ready connections and a hostname answering 1033 means the
record is not a proxied CNAME to this tunnel. `dig` cannot tell a flattened
apex CNAME from any proxied A record; read the record through the API. A
record pinned as the SaaS fallback origin cannot be deleted until that
designation is removed.
