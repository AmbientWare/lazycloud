# Production deployment

Terraform builds the infrastructure (deploy/terraform), Ship publishes a
version, Deploy records it on the `prod` branch, and Argo CD syncs the chart
(`deploy/helm/lazycloud`) from that branch into namespace `lazycloud-prod`.

## Ship and roll back

| Command | What it does |
| --- | --- |
| `gh workflow run ship.yml -f bump=patch` (or `minor`, `major`) | Tags the next version on main, runs every check, pushes the server, scheduler and web images, publishes the CLI to PyPI, then calls Deploy |
| `gh workflow run deploy.yml -f version=<x.y.z>` | Writes `prod` as that version's tree plus `values-deployment.yaml`; an earlier version rolls back. Migrations do not roll back |
| `gh workflow run node-images.yml` | Bakes the CPU and GPU node images into every fleet region; its summary holds the `LAZYCLOUD_FLEET_IMAGES` value for the env file |

Some releases need a step before Ship. New scheduler IAM applies
`platform-deployment` first; it only adds permissions. A change to
`deploy/ami` runs Node images from main and lands a PR updating
`LAZYCLOUD_FLEET_IMAGES`.

## Settings and secrets

"Values" is platform-deployment's `values.json`, which Deploy merges into the
chart. "Env file" is `deploy/helm/lazycloud/environments/prod.yaml`. The
platform document is Secrets Manager `lazycloud-prod/platform`, which
Terraform writes; the operator document is `lazycloud-prod/operator`, which
the operator writes and Terraform only reads. All variables carry the
`LAZYCLOUD_` prefix.

| Setting | From | Read by |
| --- | --- | --- |
| `DATABASE_URL`, `DATABASE_SESSION_URL` | platform document | server, scheduler, migrate (publish-agent-release reads the first) |
| `SECRETS_MASTER_KEY` | platform document, as file `secrets.key` (`SECRETS_KEY_FILE`) | server, scheduler |
| `STRIPE_WEBHOOK_SECRET` | platform document | server |
| `CLOUDFLARE_TUNNEL_CREDENTIALS` | platform document, as file `credentials.json` | cloudflared |
| `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET` | operator document | server |
| `STRIPE_API_KEY` | operator document | server, scheduler |
| `RESEND_WEBHOOK_SECRET` | operator document | server |
| `RESEND_API_KEY` | operator document | scheduler |
| `CLOUDFLARE_API_TOKEN` (custom domains) | operator document | server |
| `TCP_DNS_API_TOKEN` (DNS edit on the zone) | operator document | cert-manager |
| Listen addresses, `PUBLIC_URL`, `EDGE_URL`, `EDGE_TCP_URL`, `EDGE_TCP_CERT`/`_KEY`, `AGENT_SERVER_ADDR`, `INSTALL_URL`, `CLIENT_RELEASE_VERSION`, `DRAIN_DELAY`, telemetry | chart | server; the scheduler takes `AGENT_SERVER_ADDR` and `INSTALL_URL` for user data |
| Object store, workspace buckets, ECR registry and host role, Cloudflare zone, fleet account, networks, node role and instance profile, `AWS_REGION` | values | server, scheduler |
| `FLEET_IMAGES`, `FLEET_MAX_HOSTS`, `FLEET_IDLE_TIMEOUT`, `LOG_FORMAT` | env file | server, scheduler |
| `AGENT_DIST_DIR` | server image | server |
| Agent `--server`, `--gateway`, `--cloud-host-id`, `--agent-version`, `--agent-sha256` | launcher user data | agent on fleet hosts |
| `OCI_RUNTIME=runsc` | node image | agent on fleet hosts |

AWS credentials come from Pod Identity, ECR logins included. `deploy/check.sh`
fails when the chart gives a process a variable its binary does not read.

## Bring-up from scratch

Run applies from an operator machine with `AWS_PROFILE=default` after
`aws sts get-caller-identity` names the platform account. Before step 1, make
the state bucket and backend file (deploy/terraform/README.md), the operator
document in us-east-1 and the account's GitHub OIDC provider.

1. Apply `platform-core` with `cluster_api_cidrs` set to your address.
2. Apply `platform-deployment`. **Care:** it needs the operator document and
   a PlanetScale organization.
3. Apply `github`.
4. Run Node images and commit its `LAZYCLOUD_FLEET_IMAGES` into the env file.
5. Run Ship.
6. **Care:** apply `platform-deployment` again with `host_load_balancer` and
   `tcp_load_balancer` set to the hostnames from `kubectl -n lazycloud-prod
   get svc server-hosts server-tcp`. They exist only after the first sync.
7. Create the first administrator: `kubectl -n lazycloud-prod exec
   deploy/server -- /usr/local/bin/server admin create-user --email <owner>
   --admin`, then sign in with GitHub and provision billing through the
   service.

## Constraints

- LISTEN, the migration, metering and leader locks need a connection that
  keeps its session: `LAZYCLOUD_DATABASE_SESSION_URL`, a direct endpoint
  rather than a transaction pooler.
- Node capacity installs before Argo CD, whose pre-install hooks need a node.
- Argo CD reads `deploy/argocd/apps` from main, and the deployment from `prod`.
- Only the prod environment's deploy key pushes `prod`; Deploy refuses a tag
  that is not on main.
- A region missing from `LAZYCLOUD_FLEET_IMAGES` launches no hosts.
- The server reads the TCP certificate at start, so a renewal waits for the
  next release or restart.
- Agent archives and images are linux/amd64 only.
- Real-EC2 fleet acceptance runs in `AWS_PROFILE=default` with
  `LAZYCLOUD_EC2_ACCEPTANCE_PROFILE`; `default-test` serves only the
  connected-account checks.
