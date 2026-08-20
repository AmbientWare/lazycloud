# Cloudflare Deployment

This Terraform configuration owns the public ingress tunnel and the two DNS
records that point at it, plus optionally the Cloudflare for SaaS fallback
origin. It owns nothing else in the zone, deliberately.

## What it does not own, and must never take

The zone carries records this platform did not create and cannot replace. On
`lazycloud.dev` that is five `MX` records, an SPF `TXT`, and a Google
site-verification `TXT` — live mail, coexisting with a proxied apex CNAME only
because of CNAME flattening. `deploy/AGENTS.md` states the rule: never clear the
zone; delete records by id.

There is therefore no zone resource here and no record set. Every record is named
individually. A plan that proposes destroying anything this module did not
declare is a bug in the module or a mistake in the import, and must be stopped
rather than applied.

The API tokens are also not managed here. Minting a token needs a token, and the
resulting cycle is not worth automating; they stay dashboard-created and are
documented in `deploy/RUNBOOK.md`.

## Declared, not adopted

An apply against a zone that has none of these creates all of them: the tunnel,
its secret, both records, and optionally the fallback origin. There is no import
path here and there should not be one. A resource Terraform adopted carries
whatever a human left on it, and the difference between "declared" and "happens
to match" is invisible until an apply strips something nobody declared.

`tunnel_secret` is the clearest case. It is an argument rather than something the
API returns, so an adopted tunnel has a secret Terraform does not know and the
first apply rotates it out from under the running connector. Created here, the
secret is Terraform's from the start.

### Replacing resources a human made

Delete them through the API, then apply. Do not import them.

Order matters, and one step is not obvious:

1. **Remove the Cloudflare for SaaS fallback origin designation first**, under
   SSL/TLS → Custom Hostnames, if the apex is set as one. A record pinned that
   way cannot be deleted at all, and the delete fails with an error about the
   record rather than about the designation.

2. **Delete the two CNAMEs by id.** Never clear the zone:

   ```sh
   curl -sX DELETE -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
     "https://api.cloudflare.com/client/v4/zones/$ZONE_ID/dns_records/$RECORD_ID"
   ```

   `lazycloud.dev` carries live Google Workspace mail — five `MX`, an SPF `TXT`,
   and a site-verification `TXT` — which coexist with a proxied apex CNAME only
   because of CNAME flattening. Read the zone through the API before deleting
   anything; `dig` cannot tell a flattened apex CNAME from an unrelated proxied
   A record.

3. **Delete the tunnel**, then apply. The new tunnel has a new id, so the
   records this module creates point at it correctly, but two things outside this
   module name the old one: `deploy/public-ingress/cloudflared.yml` and the
   credentials file it loads. Both need replacing before `public-ingress` comes
   back up.

The public host is down from the moment the records go until the new connector is
running. Plan for that rather than discovering it: the edge serves 1033 for a
hostname whose record no longer resolves to a live tunnel, and 1033 also looks
exactly like a connector that is merely unhealthy.

## Backend and credentials

Terraform state contains secrets — here, the tunnel secret in full. Supply a
standard remote backend owned by the deployment operator; this repository
deliberately does not create or delete its bucket. Keep backend coordinates and
credentials outside the repository.

Authenticate AWS and Cloudflare with short-lived operator credentials. Do not put
credentials in `*.tfvars`:

Set `AWS_PROFILE` to a profile whose credentials can reach the state bucket
directly. A profile that assumes a role the current identity cannot assume fails
at `init`, before Terraform reaches Cloudflare at all, and the error names STS
rather than the backend.

```sh
export AWS_PROFILE=platform-operations
export CLOUDFLARE_API_TOKEN=temporary-operator-token

terraform -chdir=deploy/cloudflare init \
  -backend-config="bucket=$CLOUDFLARE_STATE_BUCKET" \
  -backend-config="key=$CLOUDFLARE_STATE_KEY" \
  -backend-config="region=$CLOUDFLARE_STATE_REGION" \
  -backend-config="encrypt=true" \
  -backend-config="use_lockfile=true"
```

The operator token needs Account > Cloudflare Tunnel > Edit, Zone > DNS > Edit,
and Zone > SSL and Certificates > Edit. Zero Trust permissions are separate from
the general account ones and are not included by an account-wide token template —
a token without them lists zero tunnels and returns `1001 Not authorized` on a
tunnel that demonstrably exists, which reads like a missing resource rather than
a missing permission. That is deliberately wider than
`LAZYCLOUD_CLOUDFLARE_API_TOKEN`, which the control plane runs with and which
only needs the SSL scope. They are two tokens; only the narrow one belongs in a
deployment `.env`.

## Plan and apply

Copy `terraform.tfvars.example` into a secure operator directory and review the
confirmations. Both default to `false`, so a bare apply refuses.

```sh
terraform -chdir=deploy/cloudflare fmt -check
terraform -chdir=deploy/cloudflare validate
terraform -chdir=deploy/cloudflare plan -out=cloudflare.tfplan
terraform -chdir=deploy/cloudflare apply cloudflare.tfplan
```

Before applying, read the zone's records and compare them afterwards:

```sh
curl -s -H "Authorization: Bearer $CLOUDFLARE_API_TOKEN" \
  "https://api.cloudflare.com/client/v4/zones/$ZONE/dns_records?per_page=100" \
  | python -c 'import json,sys; [print(r["type"], r["name"], r["content"]) for r in json.load(sys.stdin)["result"]]'
```

The `MX`, SPF, and verification records must be byte-identical either side. That
comparison is the acceptance for this module; a green apply is not.

## After applying

Two values move by hand, the same way the tailnet module's do:

| Output | Destination |
| --- | --- |
| `tunnel_id` | the `tunnel:` line in `deploy/public-ingress/cloudflared.yml` |
| `tunnel_credentials` | the file `LAZYCLOUD_PUBLIC_INGRESS_CREDENTIALS_FILE` names, `chmod 0444`, in a directory `chmod 0700` |

Then recreate `public-ingress` and confirm the public URL answers and an existing
custom hostname still resolves. A tunnel that is healthy in the dashboard proves
the connector registered, not that anything routes.

## Fallback origin

`manage_fallback_origin` is off by default. Every active custom hostname resolves
through the fallback origin, so a wrong value takes a customer's domain down
rather than this platform's. Turn it on only after confirming the value it would
write matches what the zone already holds.
