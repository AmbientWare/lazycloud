# Operate public HTTP ingress

`cloudflared` forwards public HTTP traffic to `control-plane:9000`.
The tunnel is locally managed; `cloudflared.yml` owns its routes.
Adding hostnames in the Cloudflare dashboard does not change those routes.

Use [the Terraform module](../cloudflare/README.md) to create or change tunnel
resources. Use this guide for routing, credentials, and diagnosis.

## Check the routing rules

Rules are ordered and first match wins. On the apex hostname, the edge refuses
`/metrics` and `/worker-repository/` before forwarding other requests.

Generated workload hostnames forward all paths. The API rewrites those hosts
to the workload's handler. The final rule forwards customer-owned hostnames
that cannot be enumerated in the tunnel configuration.

Keep authorization at the origin. An edge rule only controls that ingress
path; internal callers and other hostnames still reach the API.

```sh
curl -s -o /dev/null -w '%{http_code}\n' https://lazycloud.dev/metrics
```

Expect 404. This alone does not prove edge filtering because the origin also
returns 404 for this path.

## Enable customer domains

In Cloudflare for SaaS, enable Custom Hostnames and configure the fallback
origin. Set `LAZYCLOUD_CLOUDFLARE_API_TOKEN` and
`LAZYCLOUD_CLOUDFLARE_ZONE_ID` for the consuming services through their normal
secret bindings. The workload token needs SSL and Certificates edit access;
it is separate from the Terraform operator token.

Customers register a hostname and add the DNS records returned by LazyCloud.
Their CNAME must be DNS-only if their zone also uses Cloudflare. A proxied
record can keep traffic in their zone instead of reaching this custom hostname.

Inspect a pending hostname using the operator's token and zone ID:

```sh
curl --fail --show-error -s \
  -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
  "https://api.cloudflare.com/client/v4/zones/$CLOUDFLARE_ZONE_ID/custom_hostnames" \
  | jq '.result[] | {hostname, status, verification_errors, ssl: .ssl.status}'
```

Read verification errors before changing DNS. A missing fallback origin is
a platform configuration issue.

## Deliver credentials locally

Compose reads the tunnel credentials file named by
`LAZYCLOUD_PUBLIC_INGRESS_CREDENTIALS_FILE`. The container runs as UID 65532.
It needs file-read access, while the host parent directory must remain private:

```sh
chmod 0700 ~/.lazycloud/public-ingress
chmod 0444 ~/.lazycloud/public-ingress/credentials.json
```

In Kubernetes, use the chart's secret bindings. Keep tunnel secrets outside Git.

## Rotate a tunnel

Prepare a second tunnel through the owning Terraform configuration, update
only the tunnel's DNS records, and switch the connector. Verify traffic before
retiring the old tunnel and its exact credentials. Deleting the old tunnel
first leaves the hostnames without a connector.

Preserve mail, verification, and customer records. Inspect exact DNS record IDs;
never replace the zone to change ingress.

## Diagnose HTTP 1033

Read connector readiness and logs first, then inspect the tunnel and hostname
record through the Cloudflare API. A ready connector does not prove that DNS
points to its tunnel.

A proxied apex CNAME is flattened to anycast A records, so `dig` alone cannot
identify the configured target. Check for a stale tunnel ID, a deleted tunnel,
or a record retained as a SaaS fallback origin before changing routing.

Agent connections use a separate TCP load balancer and DNS-only hostname.
Use [connection gateway deployment](../connection-gateway.md) for that path.
