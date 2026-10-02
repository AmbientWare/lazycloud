# Terraform roots

| Root | State | Owns | Credentials |
| --- | --- | --- | --- |
| state | local | The state bucket | AWS |
| platform-core | `platform-core/lazycloud.tfstate` | VPC, EKS Auto Mode cluster, node capacity, Argo CD and its root Application, cluster OIDC provider, release image repositories, workload image creation template | AWS, `TF_VAR_github_app_private_key` |
| platform-deployment | `platform-deployment/<deployment>.tfstate` | Neki database and role, buckets, secret documents, fleet networks, workload identities, deploy role, tunnel and DNS, host certificate, Stripe webhook, chart values | AWS, `CLOUDFLARE_API_TOKEN`, `PLANETSCALE_SERVICE_TOKEN_ID` and `_TOKEN`, `STRIPE_API_KEY` |
| github | `github/lazycloud.tfstate` | Environments, tag rule, OIDC subject template, Ship and Node images roles | AWS, a repository administrator's `GITHUB_TOKEN` |

Apply them in that order from an operator machine with `AWS_PROFILE=default`
after `aws sts get-caller-identity` names the platform account, and review
every saved plan first. GitHub Actions never applies. The state root's
`backend` output is the backend file the others init with:

```sh
terraform -chdir=deploy/terraform/state output -json backend >"$HOME/.lazycloud/operator/terraform-backend.json"
export TF_VAR_terraform_backend_config="$HOME/.lazycloud/operator/terraform-backend.json"
terraform -chdir=deploy/terraform/<root> init -backend-config="$TF_VAR_terraform_backend_config" -backend-config="key=<state>"
```

State holds credentials (the database password, tunnel secret, master key
and webhook secret); never print or share it. platform-deployment needs a
second apply once the chart has created its load balancers: set
`host_load_balancer` and `tcp_load_balancer` to the hostnames of the
`server-hosts` and `server-tcp` Services.

`deploy/check.sh` runs `fmt -check`, `init -backend=false` with the
committed lock files and `validate` on every root; nothing there reaches a
provider.
