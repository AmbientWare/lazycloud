# Standing a deployment up, and taking it down

Four things own different parts of a deployment, and knowing which is which is
most of operating it.

| owner | what it owns |
|---|---|
| `deploy/platform-core` (Terraform, once) | the VPC, the cluster, the image repositories, the OIDC provider, the storage class, and Argo CD |
| `deploy/platform-deployment` (Terraform, per deployment) | Redis, S3, secret containers, the PlanetScale branch, the fleet network, and every identity the deployment's workloads hold |
| Argo CD, from `main` and from the deployment's branch | everything that runs in the cluster |
| The scheduler, at runtime | the Auto Scaling group and launch template for each compute unit |

The fourth is why the fleet's capacity is not in Terraform. The scheduler sets
`DesiredCapacity` from demand and reconciles every second; a second declared
owner would lose that argument on every apply.

The third is why `helm install` appears nowhere below. Argo's root Application
reads `deploy/argocd/apps` on `main`, which lists the operators every
deployment shares and one Application per deployment. That Application reads
the deployment's branch, where Deploy records the commit it built and the
release it names. A deploy is a commit.

## Standing one up

### 1. Credentials

An operator needs AWS, a Cloudflare token with Tunnel and DNS edit, a PlanetScale
service token **and its token ID**, a Stripe key, and the organisation's GitHub
App private key. Keep them outside the repository:

```sh
install -d -m 0700 ~/.lazycloud/operator
cat > ~/.lazycloud/operator/deploy.env <<'EOF'
export PLANETSCALE_SERVICE_TOKEN_ID='...'
export PLANETSCALE_SERVICE_TOKEN='...'
export CLOUDFLARE_API_TOKEN='...'
export STRIPE_API_KEY='...'
EOF
echo "export TF_VAR_github_app_private_key=\"$(cat ambientware.private-key.pem)\"" \
  >> ~/.lazycloud/operator/deploy.env
chmod 0600 ~/.lazycloud/operator/deploy.env
```

The Cloudflare token is account-scoped, so `/user/tokens/verify` rejects it while
the account endpoints accept it. Verify against `/accounts/<id>/tunnels`.

The GitHub App key goes to Terraform rather than into a secret document. Argo
needs it to read this repository, and reading this repository is how External
Secrets gets installed, so a copy behind External Secrets would be behind
itself. `deploy/platform-core` writes it straight into Argo's
repository-credentials Secret. The App is installed on the organisation with
`repository_selection: all`; its id and installation id are variables with
defaults, and the key cannot be read back from GitHub, so a lost one is
replaced rather than recovered.

### 2. The cluster, once

```sh
source ~/.lazycloud/operator/deploy.env
terraform -chdir=deploy/platform-core init \
  -backend-config="bucket=<state-bucket>" \
  -backend-config="key=platform-core/lazycloud.tfstate" \
  -backend-config="region=us-east-1"
terraform -chdir=deploy/platform-core apply
```

Its `terraform.tfvars` carries `cluster_api_cidrs`, which must include the
address this apply runs from: the Kubernetes and Helm providers reach the
cluster's API through it. A deployment apply never does, so only this step
cares where it runs. `../platform-core/README.md` has the rest.

Skip this when the cluster exists. A second deployment attaches to the same
one.

### 3. The deployment

```sh
DEPLOYMENT=lazycloud-prod
terraform -chdir=deploy/platform-deployment init \
  -backend-config="bucket=<state-bucket>" \
  -backend-config="key=platform-deployment/$DEPLOYMENT.tfstate" \
  -backend-config="region=us-east-1"
terraform -chdir=deploy/platform-deployment apply \
  -var="deployment=$DEPLOYMENT" \
  -var="github_environment=prod" \
  -var="planetscale_organization=<org>" \
  -var="state_bucket=<state-bucket>"
```

Helm `environments/<environment>.yaml` carries the instance prices, Stripe
account and WireGuard endpoint. The chart owns fleet ceilings and secret bindings.
Use the existing prod environment as the shape, with the new environment's values.

The module reads the cluster from `platform-core/lazycloud.tfstate` and refuses
a deployment whose region differs from the cluster's.

### 4. Secret values

A deployment's credentials live in three Secrets Manager entries. Each entry is
a JSON document grouped by its writer, not one entry per key.

`<deployment>/platform` is Terraform's. It holds the database URL, generated
service keys, tunnel credentials, and fleet external ID. Terraform rewrites it
on every apply. Do not edit it by hand.

`<deployment>/operator` is yours, and Terraform only declares it. Write it once,
before anything syncs:

```sh
umask 077
cat > operator.json <<'JSON'
{
  "LAZYCLOUD_TOKEN": "rt_...",
  "LAZYCLOUD_GITHUB_CLIENT_ID": "...",
  "LAZYCLOUD_GITHUB_CLIENT_SECRET": "...",
  "LAZYCLOUD_CLOUDFLARE_API_TOKEN": "...",
  "LAZYCLOUD_STRIPE_API_KEY": "...",
  "LAZYCLOUD_STRIPE_WEBHOOK_SECRET": "..."
}
JSON
aws secretsmanager put-secret-value \
  --secret-id "$(terraform -chdir=deploy/platform-deployment output -raw operator_secret)" \
  --secret-string "file://$PWD/operator.json"
shred -u operator.json
```

`<deployment>/wireguard` belongs to the `wireguard-bootstrap` Job. The Job
generates one gateway keypair and one platform keypair per control-plane
ordinal. It writes them as one document through a role that can access only
that entry. External Secrets projects the keys as read-only files. There is no
operator value to copy and no secret per agent.

Every key is named in the chart, so one you leave out is caught when the values
render rather than by a pod that will not start.

**The administrator token is the one with a trap in it.** Generate it with
`python3 -c 'import base64,os; print("rt_" + base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode())'`.
`auth bootstrap` adopts a configured credential when it finds one and mints its
own when it does not, recording a different request id for each. Let the Job run
without this and the credential exists only inside that pod, nothing afterwards
has a bearer token, and supplying the value later is refused as an already
completed bootstrap. Use offline administrator recovery on a persistent
installation; do not reset its schema.

### 5. The GitHub environment

Each deployment has a GitHub environment of its short name, `prod` or
`staging`. The Deploy workflow runs its job under it, and the deployment's deploy
role admits that environment's token and no other. The role's ARN is a secret on
the environment, not on the repository:

```sh
gh secret set AWS_DEPLOY_ROLE_ARN --env prod \
  --body "$(terraform -chdir=deploy/platform-deployment output -raw deploy_role_arn)"
gh variable set INFRASTRUCTURE_CONFIG_URI --env prod \
  --body "$(terraform -chdir=deploy/platform-deployment output -raw infrastructure_config_uri)"
```

Required reviewers on the `prod` environment are the approval gate for
`promote.yml`; the run pauses at the deploy job until someone approves.

Nothing else. The workflow pushes to a branch in its own repository with the
token GitHub gives it, and the App credential Argo reads with is supplied to
Terraform rather than to CI. The two go in opposite directions and are not the
same grant.

### 6. The first deploy

Run the Node Images workflow once before the first Ship, and again only when its
host recipe changes. It bakes CPU and GPU images in parallel and publishes the
current catalog. Ship refuses a missing or incompatible catalog before it builds
anything.

Run the Ship workflow from `main` and choose `patch`. It cuts the version,
publishes the Python package and release, then deploys onto that release. Agent
and container-worker changes reuse the current host images. Nodes download the
agent and pull the exact worker digest before reporting ready.

`deploy.yml` on its own is the ordinary case afterwards, and runs many times
against one release: it builds the commit's images once, into the shared
repositories, and records them for the deployment it was given, carrying
forward whichever release that deployment already names. Ship deploys to prod
directly until staging runs; then a release lands on staging and
`promote.yml` carries the commit staging runs to prod with no build.

Run a deploy before there is a release and the control plane starts, reads a
price map that says managed capacity is wanted, finds no worker image, agent
binary or AMI catalog to serve it with, and refuses. That is a half-configured
deployment being rejected rather than a fault, and the way out is Ship.

Argo takes it from there, in wave order: the service accounts, the WireGuard
key bootstrap, External Secrets, the schema and billing catalog, the
administrator, the rate card, and the workloads. The Jobs that open a
database are each alone in their wave because the chart's connection budget
counts one Job's pool and refuses to render if the pools can exceed what the
server allows.

The fleet registers itself on the way past too, in the one wave after the
workloads: it registers through the public API, so the control plane has to be
serving before it can say anything. Without it the deployment accepts work and
can place it nowhere, because a pool will not launch without an account to launch
into. It is idempotent and leaves a connection already present alone, since
reconnecting mints a new authorization generation.

The billing catalog runs on every sync rather than once. Signing in provisions a
subscription and fails closed without one, so a plan shipped without its price
would otherwise be found by a person who could not sign in. It is additive: a
published account gets nothing new, and a price whose amount disagrees with the
repository fails the Job instead of being edited, because customers are already
billed against the published one.

The rate card runs the same way, in a wave of its own behind the schema it
writes into. Its boundary is `billing.ratesEffectiveAt` in `deploy/chart/values.yaml`
rather than the clock: a rate is a figure customers are charged either side of,
and one taken from whenever a sync ran would open a new boundary on every sync.
A card already published at that instant is left alone, and figures that disagree
with it fail the Job. Without this the deployment meters usage it cannot price
and fills its event log with `billing.span.unpriced` at ERROR, several a second,
while nothing is billable. Changing what a customer pays is this value and the
rate card in `packages/shared` moving together in one commit.

Watch it rather than assume it:

```sh
aws eks update-kubeconfig --name lazycloud --region us-east-1
kubectl -n argocd get applications -w
kubectl -n lazycloud-prod get pods
kubectl -n lazycloud-prod describe secretstore aws-secrets-manager
```

The store reports `Ready` once the operator has exchanged the reader's token
for the deployment's role. Anything else names the subject or the role, and is
this module's trust condition disagreeing with the chart's service account.

### 7. Ingress

Point the tunnel at the cluster once the control plane is Ready. `cloudflared`
runs in the chart with more than one connector, so the tunnel is served by the
cluster rather than by a host.

Point the DNS name in Helm `runtime.LAZYCLOUD_WIREGUARD_PUBLIC_ENDPOINT` at the deployment's UDP load
balancer on port 51820. This endpoint is separate from the Cloudflare HTTP
tunnel. Confirm an enrolled agent and a platform peer report recent WireGuard
handshakes before calling the private network ready.

## Adding staging

Repeat steps 3 to 5 with `DEPLOYMENT=lazycloud-staging`, `github_environment=staging`,
a Stripe test account, a `fleet_cidr` of its own, a `deploy/cloudflare` apply
of its own named by `cloudflare_state_key`, and `planetscale_cluster_size`
set to the development tier. Run `deploy.yml` with `staging` once to create the
`staging` branch. Only then add `deploy/argocd/apps/lazycloud-staging.yaml`
beside the prod file, reading `staging` into `lazycloud-staging`, and point
Ship's deploy at staging. An Application whose branch does not exist yet sits
in a comparison error and nothing else.

Removing a deployment file from `deploy/argocd/apps` removes the deployment,
workloads included; the Applications carry Argo's resources finalizer for that
reason.

## Taking one down

Take the fleet's capacity away first, through the control plane that owns it:

```sh
lazycloud-admin fleet destroy
```

The pool's autoscaling group and launch template are created at runtime, so
Terraform has never heard of them. Destroying the cluster first leaves an
autoscaling group launching instances with nothing left alive to stop it, and
the bill runs until somebody notices.

Deleting the unit is what removes them, and the command's success is the proof:
the route answers only once the provider reports the group and the launch
template both gone. It exits non-zero and names any unit it could not finish, so
a failure here means stop rather than continue to the destroy.

**A 409 there is expected, and is waited out rather than worked around.** The
delete contends with a capacity mutation lease held for up to five minutes and
renewed by whoever holds it, so a scheduler mid-reconcile refuses the first
attempts and clears on its own; a 503 likewise means the provider is partway
through, since deleting an autoscaling group returns before the group is gone.
The command retries both. Deleting the group in AWS instead is what fails: the
unit survives, and the scheduler rebuilds the group from it minutes later, after
the check that said the capacity was gone.

Remove the deployment's file from `deploy/argocd/apps` and let Argo prune the
namespace, then drop the branch role from state and destroy:

```sh
terraform -chdir=deploy/platform-deployment state rm planetscale_postgres_branch_role.control_plane
terraform -chdir=deploy/platform-deployment destroy -var="deployment=$DEPLOYMENT" ...
```

The role is removed rather than destroyed because Terraform cannot destroy it:
it depends on the branch, so it goes first, and PlanetScale refuses to drop a
role that still owns the tables the schema created. Deleting the branch takes
the role and the database with it. Skip the `state rm` and the destroy runs to
the end, fails on the role, and leaves you doing this anyway with the rest
already gone.

The cluster outlives the deployment. Destroy it last, and only when no
deployment remains, with `../platform-core/README.md`.

Three things survive a deployment's destroy and have to be dealt with by hand:

- **Workspace buckets.** The control plane creates `<deployment>-workspace-<uuid>`
  lazily at runtime, so Terraform never knew them and leaves one per workspace.
- **The PlanetScale database**, if a branch other than `main` was ever created.
  Deleting `main` takes the database with it, so an organisation left holding a
  database after a destroy is holding one this module did not make.
- **The operator document.** A destroy removes it and its values, so step 4 is
  done again on the next deployment. Keep a copy: the Stripe webhook signing
  secret is returned only when the endpoint is created, so it is the one value a
  provider will not show you twice.

Never reset external, deployed, or production data. Predeployment, resetting and
rebuilding is ordinary and is how the module is proven to reproduce.
