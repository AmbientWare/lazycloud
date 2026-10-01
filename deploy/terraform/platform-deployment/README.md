# Platform deployment

Apply this root once per deployment, with its own state and namespace. It
owns what a deployment cannot share: its PlanetScale database, S3 buckets,
Secrets Manager documents, regional fleet networks and node identity, the
host connection certificate, and every AWS identity its workloads hold. The
cluster, Argo CD and the reference image repositories are platform-core,
read here through its state; the tunnel and zone are the cloudflare root's.

```sh
export TF_VAR_terraform_backend_config="$HOME/.lazycloud/operator/terraform-backend.json"
terraform -chdir=deploy/terraform/platform-deployment init -backend-config="$TF_VAR_terraform_backend_config" \
  -backend-config="key=platform-deployment/lazycloud-prod.tfstate"
terraform -chdir=deploy/terraform/platform-deployment plan -out=deployment.tfplan
```

The PlanetScale provider reads `PLANETSCALE_SERVICE_TOKEN_ID` and
`PLANETSCALE_SERVICE_TOKEN`; the Cloudflare provider `CLOUDFLARE_API_TOKEN`.

## What the apply hands on

- `<deployment>/values.json` in the deploy bucket: the chart's
  infrastructure values (configuration.tf). The Deploy workflow merges it
  with the release version into `values-deployment.yaml` on the deployment
  branch, so infrastructure changes reach the cluster on the next Deploy.
- `<deployment>/platform`: the database URL (direct port 5432 with
  `pool_max_conns`), the secrets master key and the tunnel credentials. The
  operator document holds GitHub, Stripe, Resend and Cloudflare credentials;
  see tasks/deploy.md for every binding.
- `hosts_hostname`: point it at the `server-hosts` load balancer through the
  cloudflare root once the chart created it.

## Identities

The server and scheduler hold `<deployment>-control-plane` through Pod
Identity, with session tags off. It launches and terminates fleet instances
tagged `lazycloud:fleet=<deployment>` in this account, passes the fleet node
role, assumes customer connection roles and the workspace storage role, and
reads and writes the objects and workspace buckets and workload images. The
External Secrets store holds `<deployment>-secrets-reader` through IRSA,
scoped to `<deployment>/*`. GitHub's Deploy job holds `<deployment>-deploy`
(ci.tf), which reads `values.json` and the release images' metadata.

## Two deployments, one cluster

A second deployment needs its own `deployment`, a `fleet_cidr` that overlaps
neither the other's nor the cluster's, a cloudflare apply of its own with
`cloudflare_state_key` naming it, a Stripe account, and its own
`github_environment`.
