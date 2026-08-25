# Standing this up, and taking it down

Three things own different parts of a deployment, and knowing which is which is
most of operating it.

| owner | what it owns |
|---|---|
| `deploy/platform-eks` (Terraform) | the VPC, the cluster, Redis, IAM, ECR, S3, secret containers, the PlanetScale branch, the fleet network, and Argo CD |
| Argo CD, from the deployment branch | everything that runs in the cluster |
| The scheduler, at runtime | the Auto Scaling group and launch template for each compute unit |

The third is why the fleet's capacity is not in Terraform. The scheduler sets
`DesiredCapacity` from demand and reconciles every second; a second declared
owner would lose that argument on every apply.

The second is why `helm install` appears nowhere below. CI builds images and
records them on the deployment branch; Argo reconciles the cluster to that
branch. A deploy is a commit.

## Standing one up

### 1. Credentials

An operator needs AWS, a Cloudflare token with Tunnel and DNS edit, a PlanetScale
service token **and its token ID**, and a Stripe key. Keep them outside the
repository:

```sh
install -d -m 0700 ~/.lazycloud/operator
cat > ~/.lazycloud/operator/deploy.env <<'EOF'
export PLANETSCALE_SERVICE_TOKEN_ID='...'
export PLANETSCALE_SERVICE_TOKEN='...'
export CLOUDFLARE_API_TOKEN='...'
export STRIPE_API_KEY='...'
EOF
chmod 0600 ~/.lazycloud/operator/deploy.env
```

The Cloudflare token is account-scoped, so `/user/tokens/verify` rejects it while
the account endpoints accept it. Verify against `/accounts/<id>/tunnels`.

### 2. Terraform

```sh
source ~/.lazycloud/operator/deploy.env
DEPLOYMENT=lazycloud-prod
terraform -chdir=deploy/platform-eks init \
  -backend-config="bucket=<state-bucket>" \
  -backend-config="key=platform-eks/$DEPLOYMENT.tfstate" \
  -backend-config="region=us-east-1"
terraform -chdir=deploy/platform-eks apply \
  -var="deployment=$DEPLOYMENT" \
  -var="planetscale_organization=<org>" \
  -var="state_bucket=<state-bucket>"
```

`terraform.tfvars` carries the instance price map and the payment-provider
account id. Without the price map managed capacity stays off, and the symptom is
pools that never launch rather than anything that fails. Without the account id
the apply refuses, because the catalog publisher checks the credential against it
and has nothing to check.

**A failed apply is not proof that nothing was created.** EKS has returned a 400
on `CreateCluster` and created the cluster anyway, leaving it ACTIVE and absent
from state, where `terraform destroy` will never find it. After any failed apply,
check `aws eks list-clusters` before retrying.

### 3. Secret values

A deployment's credentials live in two Secrets Manager entries, each a JSON
document. Secrets Manager bills per entry, so a dozen containers was a dozen
charges for what is one set of values.

`<deployment>/platform` is Terraform's. It holds the database URL, the two
generated shared keys, the tunnel credentials and the fleet external ID, and it
is rewritten on every apply. Do not edit it by hand; the next apply will
overwrite what you wrote.

`<deployment>/operator` is yours, and Terraform only declares it. Write it once,
before anything syncs:

```sh
umask 077
cat > operator.json <<'JSON'
{
  "LAZYCLOUD_TOKEN": "rt_...",
  "LAZYCLOUD_GITHUB_CLIENT_ID": "...",
  "LAZYCLOUD_GITHUB_CLIENT_SECRET": "...",
  "LAZYCLOUD_TAILNET_OAUTH_CLIENT_ID": "...",
  "LAZYCLOUD_TAILNET_OAUTH_CLIENT_SECRET": "...",
  "LAZYCLOUD_CLOUDFLARE_API_TOKEN": "...",
  "LAZYCLOUD_STRIPE_API_KEY": "...",
  "LAZYCLOUD_STRIPE_WEBHOOK_SECRET": "..."
}
JSON
aws secretsmanager put-secret-value \
  --secret-id "$(terraform -chdir=deploy/platform-eks output -raw operator_secret)" \
  --secret-string "file://$PWD/operator.json"
shred -u operator.json
```

Every key is named in the chart, so one you leave out is caught when the values
render rather than by a pod that will not start.

**The administrator token is the one with a trap in it.** Generate it with
`python3 -c 'import base64,os; print("rt_" + base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode())'`.
`auth bootstrap` adopts a configured credential when it finds one and mints its
own when it does not, recording a different request id for each. Let the Job run
without this and the credential exists only inside that pod, nothing afterwards
has a bearer token, and supplying the value later is refused as an already
completed bootstrap. The way back is resetting the schema.

**The GitHub App private key** is in neither document. Argo needs it to read this
repository, and reading this repository is how External Secrets gets installed,
so a copy behind External Secrets would be behind itself. Terraform writes it
straight into Argo's repository-credentials Secret, from the operator
environment:

```sh
echo "export TF_VAR_github_app_private_key=\"$(cat ambientware.private-key.pem)\"" \
  >> ~/.lazycloud/operator/deploy.env
```

The App is installed on the organisation with `repository_selection: all`, so
this one key reaches every repository Argo is later pointed at. Its id and
installation id are Terraform variables with defaults; the key is generated in
the App's settings and cannot be read back from GitHub, so a lost one is replaced
rather than recovered.

### 4. Repository variables

```sh
gh secret set AWS_DEPLOY_ROLE_ARN --body "$(terraform -chdir=deploy/platform-eks output -raw deploy_role_arn)"
gh secret set TF_STATE_BUCKET --body '<state-bucket>'
```

The deploy role's trust names `repo:<owner>/<repo>:environment:production`, so
the workflow must keep `environment: production` or it cannot assume the role.

Nothing else. The workflow pushes to a branch in its own repository with the
token GitHub gives it, and the App credential Argo reads with is supplied to
Terraform rather than to CI -- the two go in opposite directions and are not the
same grant.

### 5. The first deploy

```sh
gh workflow run ship.yml -f deployment=lazycloud-prod
```

`ship` publishes the release and then deploys onto it, handing the manifest URL
from the first half to the second. Do that for a first bring-up, and for any
change to the agent, the container-worker image, or the node AMI. It bakes the
image every time and takes about fifteen minutes, because the image carries the
agent and worker this release publishes and one baked earlier describes an
earlier release.

`deploy` on its own is the ordinary case afterwards, and runs many times against
one release: it builds the control-plane images, tags them with the commit, and
pushes the rendered values to the deployment branch, carrying forward whichever
release is already recorded.

Run it before there is a release and the control plane starts, reads a price map
that says managed capacity is wanted, finds no worker image, agent binary or AMI
catalog to serve it with, and refuses. That is a half-configured deployment being
rejected rather than a fault, and the way out is `ship`.

Argo takes it from there, in wave order: the storage class and service accounts,
the secrets, then the schema and the billing catalog, then the administrator, and
the workloads last. Nothing waits on a workload, so one that cannot start fails
by itself instead of holding up the Job that would fix it.

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

Watch it rather than assume it:

```sh
aws eks update-kubeconfig --name lazycloud-prod --region us-east-1
kubectl -n argocd get applications -w
kubectl -n lazycloud get pods
```

### 6. Ingress

Point the tunnel at the cluster once the control plane is Ready. `cloudflared`
runs in the chart with more than one connector, so the tunnel is served by the
cluster rather than by a host.

## Taking one down

Take the fleet's capacity away first, through the control plane that owns it:

```sh
WORKSPACE_ID=<platform workspace> lazycloud-admin unit delete <capacity owner id>
```

The pool's autoscaling group and launch template are created at runtime, so
Terraform has never heard of them. Destroying the cluster first leaves an
autoscaling group launching instances with nothing left alive to stop it, and
the bill runs until somebody notices.

Then drop the branch role from state and destroy:

```sh
terraform -chdir=deploy/platform-eks state rm planetscale_postgres_branch_role.control_plane
terraform -chdir=deploy/platform-eks destroy -var="deployment=$DEPLOYMENT" ...
```

The role is removed rather than destroyed because Terraform cannot destroy it:
it depends on the branch, so it goes first, and PlanetScale refuses to drop a
role that still owns the tables the schema created. Deleting the branch takes
the role and the database with it. Skip the `state rm` and the destroy runs to
the end, fails on the role, and leaves you doing this anyway with ninety
resources already gone.

The destroy also leaves detached volumes behind, one per dynamic claim the
cluster provisioned. They bill until removed:

```sh
aws ec2 describe-volumes --filters Name=status,Values=available \
  --query 'Volumes[].[VolumeId,Size,Tags[?Key==`Name`]|[0].Value]' --output text
```

Three things survive it and have to be dealt with by hand:

- **Workspace buckets.** The control plane creates `<deployment>-workspace-<uuid>`
  lazily at runtime, so Terraform never knew them and leaves one per workspace.
- **The PlanetScale database**, if a branch other than `main` was ever created.
  Deleting `main` takes the database with it, so an organisation left holding a
  database after a destroy is holding one this module did not make.
- **The operator document.** A destroy removes it and its values, so step 3 is
  done again on the next deployment. Keep a copy: the Stripe webhook signing
  secret is returned only when the endpoint is created, so it is the one value a
  provider will not show you twice.

Never reset external, deployed, or production data. Predeployment, resetting and
rebuilding is ordinary and is how the module is proven to reproduce.
