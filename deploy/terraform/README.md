# Terraform roots

| Root | State | Owns | Credentials |
| --- | --- | --- | --- |
| platform-core | `platform-core/lazycloud.tfstate` | VPC, EKS Auto Mode cluster, node capacity, Argo CD and its root Application, cluster OIDC provider, release image repositories, workload image creation template, NetworkPolicy enforcement | AWS, `TF_VAR_github_app_private_key` |
| platform-deployment | `platform-deployment/<deployment>.tfstate` | Postgres database and role, buckets, the layer bucket and its regional copies, secret documents, fleet networks, workload identities, deploy role, tunnel and DNS, host certificate, Stripe webhook, chart values | AWS, `CLOUDFLARE_API_TOKEN`, `PLANETSCALE_SERVICE_TOKEN_ID` and `_TOKEN`, `STRIPE_API_KEY` |
| github | `github/lazycloud.tfstate` | Environments, tag and prod branch rules, the prod deploy key, OIDC subject template, Ship and Node images roles | AWS, a repository administrator's `GITHUB_TOKEN` |

Apply them in that order from an operator machine with `AWS_PROFILE=default`
after `aws sts get-caller-identity` names the platform account, and review
every saved plan first. GitHub Actions never applies.

State lives in `lazycloud-terraform-state-<account>` in us-east-1, which
the operator owns and no root manages: versioned, public access blocked,
encrypted. A new account needs that bucket made by hand first. Each root
inits from the operator's backend file:

```sh
cat >"$HOME/.lazycloud/operator/terraform-backend.json" <<'JSON'
{"bucket": "lazycloud-terraform-state-534742592531", "region": "us-east-1", "profile": "default", "encrypt": true, "use_lockfile": true}
JSON
export TF_VAR_terraform_backend_config="$HOME/.lazycloud/operator/terraform-backend.json"
terraform -chdir=deploy/terraform/<root> init -backend-config="$TF_VAR_terraform_backend_config" -backend-config="key=<state>"
```

State holds credentials (the database password, tunnel secret, master key
and webhook secret); never print or share it. platform-deployment needs a
second apply once the chart has created its load balancers: set
`host_load_balancer` and `tcp_load_balancer` to the hostnames of the
`server-hosts` and `server-tcp` Services.

`local.fleet_regions` in `platform-deployment/fleet.tf` lists the regions the
fleet buys in. Each gets a fleet network, and each region other than the
deployment's own gets a copy of the layer bucket, which S3 replication fills.
Adding a region takes one entry there, node images for it and an apply.

`deploy/check.sh` runs `fmt -check`, `init -backend=false` with the
committed lock files and `validate` on every root; nothing there reaches a
provider.
