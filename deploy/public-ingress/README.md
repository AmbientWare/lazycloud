# Public ingress

`cloudflared` runs as its own service and reaches the origin over the Compose
network at `control-plane:9000`. Nothing listens on a public port, TLS
terminates at Cloudflare's edge, and the deployment holds no certificate.

The tunnel is **locally managed**: it is created with `config_src: local`, so
Cloudflare pushes no configuration and `cloudflared.yml` in this directory is
the only statement of what is exposed. Public hostnames added in the dashboard
have no effect on a tunnel in this mode.

## What is exposed

`cloudflared.yml` is an ordered allowlist, first match wins. The apex forwards
everything except two prefixes refused before the origin is dialed:

| Refused at the edge | Why |
| --- | --- |
| `/metrics` | No route serves this. The rule stays so the path cannot be exposed if one returns. |
| `/worker-repository/` | Worker RPC. Workers dial the internal or WireGuard origin, never the public one. |

Both are also authorized at the origin — the edge rule removes the surface, it
does not replace the check. Verify with a request that the origin would answer
differently:

```sh
curl -s -o /dev/null -w '%{http_code}\n' https://lazycloud.dev/metrics   # 404 at edge, and 404 at the origin too
```

`*.lazycloud.dev` forwards **all** paths: the control plane's host-routing
middleware rewrites generated invoke hosts to the stub's handler path, so a
path filter there would break user applications.

The final rule forwards every remaining hostname, because a customer-owned
domain arrives carrying its own `Host` header and cannot be enumerated here.
The two prefixes above are refused at the edge only for `lazycloud.dev`; both
also require authorization at the origin, which is what actually protects them.
The origin cannot refuse by hostname, because internal callers reach the control
plane by service name and so arrive on a host that is not the public domain
either.

## Customer-owned domains

Cloudflare for SaaS issues a certificate per registered domain and proxies it to
this tunnel. One-time setup on the zone:

1. SSL/TLS → Custom Hostnames: enable it, and set the fallback origin to
   `lazycloud.dev`. Cloudflare forbids a *custom hostname* equal to the zone
   name; an apex fallback origin is fine.
2. Mint an API token scoped to Zone → SSL and Certificates → Edit, and set
   `LAZYCLOUD_CLOUDFLARE_API_TOKEN` and `LAZYCLOUD_CLOUDFLARE_ZONE_ID` in the
   deployment `.env`. Compose passes both to the control plane and the scheduler.

A workspace then registers an exact hostname such as `app.example.com`, publishes
the CNAME the platform reports, and claims that hostname with `domain=` on the
resource. Without the two variables the stack still serves every platform
hostname; only the domain operations fail, and they name what is missing.

The customer's CNAME has to be **DNS-only**. If their domain is also on
Cloudflare it is easy to leave the record proxied, and a proxied record is served
from their own zone and never reaches the custom hostname here, so verification
sits pending with nothing on this side to see. Cloudflare reports the cause on the
custom hostname itself:

```sh
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://api.cloudflare.com/client/v4/zones/$ZONE/custom_hostnames" \
  | jq '.result[] | {hostname, status, verification_errors, ssl: .ssl.status}'
```

`fallback origin is not active yet` means the zone setting above is missing, not
that the customer did anything wrong.

## Minting a tunnel

Requires a Cloudflare API token with Account → Cloudflare Tunnel → Edit, Zone →
DNS → Edit, and Zone → Zone → Read. Export it as `CLOUDFLARE_API_TOKEN` in the
operator's shell. It is not a deployment value: nothing in the stack reads it and
it does not belong in `.env`.

`deploy/cloudflare` now declares the tunnel and both records, and is the
preferred path. The manual steps below remain the reference for what that module
produces, and for operating a deployment that predates it.

1. `POST /accounts/{account}/cfd_tunnel` with a fresh base64 32-byte
   `tunnel_secret` and `config_src: local`.
2. Write `{"AccountTag", "TunnelID", "TunnelSecret"}` to the credentials file
   and point `LAZYCLOUD_PUBLIC_INGRESS_CREDENTIALS_FILE` at it.
3. Create two **proxied** `CNAME` records — `lazycloud.dev` and `*` — both
   targeting `<tunnel-id>.cfargotunnel.com`.
4. Put the tunnel id in `cloudflared.yml`. It is an identifier, not a secret.

The zone carries live Google Workspace mail (`MX`, SPF `TXT`,
site-verification `TXT`). Add and delete records by id; never clear the zone.
The apex `CNAME` coexists with those records only through Cloudflare's CNAME
flattening.

## Credentials file permissions

The container runs as uid **65532**, so a credentials file owned by the
operator at mode `0400` is unreadable inside it and the tunnel fails to start.
The file is `0444` and its parent directory is `0700`: the directory carries
the restriction, so no other host user can traverse to the file.

```sh
chmod 0700 ~/.lazycloud/public-ingress
chmod 0444 ~/.lazycloud/public-ingress/credentials.json
```

## Rotation

A tunnel secret cannot be changed in place. Mint a second tunnel, repoint both
DNS records at it, recreate `public-ingress`, then delete the old tunnel once
no connector is registered against it. Deleting the tunnel first strands the
hostnames on a route with no connector and the edge returns 1033.

## When the edge returns 1033

The connector is healthy and the hostname is not reaching this tunnel. `1033`
is the tunnel-routing error, so the request did reach Cloudflare's tunnel
layer; a proxied record aimed at a non-tunnel origin returns 521/522/523
instead. Read the zone through the API — `dig` cannot help, because a proxied
apex `CNAME` is flattened to anycast A records and looks identical to an
unrelated proxied A record.

Two causes have occurred here: a record left pinned as a Cloudflare for SaaS
fallback origin (SSL/TLS → Custom Hostnames), which cannot be deleted and which
auto-created tunnel DNS silently declines to overwrite; and a deleted tunnel,
which the connector reports as a control-stream rejection
(`Application error 0x0 (remote)`) while every network precheck passes.
