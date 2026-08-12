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

## The tunnel and the records already exist

This module is being introduced against infrastructure that a human created by
hand. **The first action is an import, not an apply.** The tunnel id currently
committed in `deploy/public-ingress/cloudflared.yml` and the two CNAMEs in the
zone are the resources to adopt:

```sh
terraform -chdir=deploy/cloudflare import \
  cloudflare_zero_trust_tunnel_cloudflared.public_ingress '<account_id>/<tunnel_id>'
terraform -chdir=deploy/cloudflare import cloudflare_dns_record.apex     '<zone_id>/<record_id>'
terraform -chdir=deploy/cloudflare import cloudflare_dns_record.wildcard '<zone_id>/<record_id>'
```

Record ids come from `GET /zones/<zone_id>/dns_records`.

One thing the import cannot recover: `tunnel_secret` is an argument rather than
something the API returns, so an imported tunnel has a secret Terraform does not
know, and the first apply rotates it.

Observed on the real account: the tunnel is **updated in place**, not replaced.
Its id survives, so `deploy/public-ingress/cloudflared.yml` needs no edit and the
DNS records do not move. What does happen is that the running connector's
credentials stop being valid the moment the secret rotates, and the edge serves
1033 until a new credentials file is written and `public-ingress` is recreated.
Have the replacement credentials ready to install before applying rather than
after; the gap is however long that takes.

The records themselves adopt cleanly, with one catch worth knowing: anything set
on them that this module does not declare — a `comment`, most likely — shows up
in the plan as a deletion. Declare it here instead of letting an apply quietly
strip it.

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
