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

`terraform.tfvars` carries the instance price map. Without it managed capacity
stays off, and the symptom is pools that never launch rather than anything that
fails.

**A failed apply is not proof that nothing was created.** EKS has returned a 400
on `CreateCluster` and created the cluster anyway, leaving it ACTIVE and absent
from state, where `terraform destroy` will never find it. After any failed apply,
check `aws eks list-clusters` before retrying.

### 3. Secret values

Terraform declares every secret and fills only the ones it generates: the
database URL, the fleet external ID, the backend route key, and the tunnel
credentials it reads from `deploy/cloudflare`. The rest come from outside and are
written once:

```sh
aws secretsmanager put-secret-value --secret-id "$DEPLOYMENT/github-client-id" --secret-string '...'
# github-client-secret, tailnet-oauth-client-id, tailnet-oauth-client-secret,
# cloudflare-api-token, stripe-api-key, stripe-webhook-secret
```

Two of them are not optional and are not obvious.

**The administrator credential, before the first sync.**

```sh
aws secretsmanager put-secret-value --secret-id "$DEPLOYMENT/administrator-token" \
  --secret-string "rt_$(python3 -c 'import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode())')"
```

`auth bootstrap` adopts a configured credential when it finds one and mints its
own when it does not, recording a different bootstrap request id for each. Let
the Job run without this and the credential exists only inside that pod, nothing
afterwards has a bearer token, and supplying the value later is refused as an
already completed bootstrap. The way back is resetting the schema.

**The GitHub App private key**, which is how Argo reads the repository:

```sh
aws secretsmanager put-secret-value --secret-id "$DEPLOYMENT/github-app-private-key" \
  --secret-string "$(cat ambientware.private-key.pem)"
```

The App is installed on the organisation with `repository_selection: all`, so
this one key reaches every repository. Its id and installation id are Terraform
variables with defaults; the key itself is generated in the App's settings and
cannot be read back from GitHub.

A secret with no value is otherwise normal rather than fatal. An unchosen
telemetry backend does not stop the control plane from serving.

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
gh workflow run release.yml -f deployment=lazycloud-prod
gh workflow run deploy-eks.yml -f deployment=lazycloud-prod
```

The release publishes the agent, the container-worker image and the node AMI. The
deploy builds the control-plane images, tags them with the commit, and pushes the
rendered values to the deployment branch. Argo takes it from there: External
Secrets first, then the chart, then the bootstrap Jobs in wave order.

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

```sh
terraform -chdir=deploy/platform-eks destroy -var="deployment=$DEPLOYMENT" ...
```

Three things survive it and have to be dealt with by hand:

- **Workspace buckets.** The control plane creates `<deployment>-workspace-<uuid>`
  lazily at runtime, so Terraform never knew them and leaves one per workspace.
- **The PlanetScale role**, if the branch is destroyed after it. The role owns
  every table the schema created and cannot be dropped while it does; destroying
  the branch takes the role with it, so let the branch go first.
- **Externally-sourced secrets.** A destroy removes the containers and their
  values, so the eight written in step 3 have to be written again.

Never reset external, deployed, or production data. Predeployment, resetting and
rebuilding is ordinary and is how the module is proven to reproduce.
