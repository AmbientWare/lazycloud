# Standing a deployment up, and taking it down

This document covers one thing: the whole life of a LazyCloud deployment's own
infrastructure, from an empty AWS account to a serving platform and back to
nothing. It is deliberately not folded into the operator runbook — that describes
running a deployment, this describes creating and destroying one.

Everything here has been executed end to end. Where a step has a sharp edge, the
edge is named rather than left to be discovered.

## What owns what

Three things provision, and the boundaries matter:

| Owner | What it creates |
| --- | --- |
| `deploy/platform-aws` (Terraform) | VPCs, the control-plane host, IAM, ECR, S3, secret containers, the PlanetScale branch, the fleet network and connection role |
| The `Deploy` workflow | Container images, the deployment bundle, converging the host, registering the fleet |
| The scheduler, at runtime | The Auto Scaling group and launch template for each compute unit |

The third is why the fleet's capacity is not in Terraform. The scheduler sets
`DesiredCapacity` from demand and reconciles every second; a second declared
owner would lose that argument on every apply. Terraform owns the network those
nodes launch into, and stops there.

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
the account endpoints accept it. Verify against
`/accounts/<id>/tunnels`, not the token endpoint.

### 2. Apply

```sh
source ~/.lazycloud/operator/deploy.env
DEPLOYMENT=lazycloud-prod
terraform -chdir=deploy/platform-aws init \
  -backend-config="bucket=<state-bucket>" \
  -backend-config="key=platform-aws/$DEPLOYMENT.tfstate" \
  -backend-config="region=us-east-1"
terraform -chdir=deploy/platform-aws apply \
  -var="deployment=$DEPLOYMENT" \
  -var="planetscale_organization=<org>" \
  -var="state_bucket=<state-bucket>"
```

The state key carries the deployment name. Two deployments sharing one key share
one state, and the second apply destroys the first.

`planetscale_cluster_size` wants the provider- and architecture-qualified name,
`PS_10_AWS_ARM`. The organization's own SKU list spells it `PS_10`, which the
provider rejects.

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

The administrator credential belongs with them, and has to be written **before
the first converge**:

```sh
aws secretsmanager put-secret-value --secret-id "$DEPLOYMENT/administrator-token" \
  --secret-string "rt_$(python3 -c 'import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode())')"
```

Not merely convenient. `auth bootstrap` adopts a configured credential when it
finds one and mints its own when it does not, and the two record different
bootstrap request ids. Converge once without this and the minted credential
exists only in a volume on the host, every later compose step has no bearer token
— `fleet ensure` among them, so no managed capacity is ever registered — and
supplying the token afterwards is refused with *"administrator bootstrap is
already complete; use offline recovery"*. Recovery mints its own token too, so
the way back is to reset the schema.

A secret with no value is not fatal. The host writes an empty variable and names
what was missing on stderr, so an unchosen telemetry backend does not stop the
control plane from serving.

### 4. Repository secrets

```sh
gh secret set AWS_DEPLOY_ROLE_ARN --body "$(terraform -chdir=deploy/platform-aws output -raw deploy_role_arn)"
gh secret set TF_STATE_BUCKET --body '<state-bucket>'
```

The deploy role's trust names `repo:<owner>/<repo>:environment:production`, so the
workflow must keep `environment: production` or it cannot assume the role.

### 5. Ship

```sh
gh workflow run ship.yml -f deployment=lazycloud-prod
```

`Ship` publishes a release, then deploys onto it. Both halves matter on a new
deployment: `Deploy` on its own names whichever release the deployment already
records, and a deployment standing up for the first time records none. A control
plane with no release serves fine and offers no managed capacity, so the symptom
is pools that never launch rather than anything that fails.

The release half builds the agent executable, the container worker, and the node
AMI, then publishes the manifest. The deploy half builds every control-plane
image in one bake, publishes the bundle, converges the host, and registers the
platform's own capacity. Roughly six minutes for the deploy, and longer for the
release when the AMI bake runs.

Afterwards, `gh workflow run deploy.yml -f deployment=lazycloud-prod` ships code
alone, carrying the same release forward.

## Taking one down

```sh
terraform -chdir=deploy/platform-aws destroy \
  -var="deployment=$DEPLOYMENT" \
  -var="planetscale_organization=<org>" \
  -var="state_bucket=<state-bucket>"
```

Two things it cannot do on its own:

**The database.** Terraform destroys the branch role before the branch it depends
on, and PlanetScale refuses a role that is still referenced, so the destroy stops
with a 422 naming the role. Delete the database, which takes both with it, then
drop the two resources from state:

```sh
curl -sX DELETE -H "Authorization: $PLANETSCALE_SERVICE_TOKEN_ID:$PLANETSCALE_SERVICE_TOKEN" \
  "https://api.planetscale.com/v1/organizations/<org>/databases/$DEPLOYMENT"
terraform -chdir=deploy/platform-aws state rm \
  planetscale_postgres_branch_role.control_plane planetscale_postgres_branch.control_plane
```

**Workspace buckets.** Each workspace gets `$DEPLOYMENT-workspace-<uuid>`, created
by the control plane rather than Terraform, so a destroy leaves them behind. They
are the one thing that outlives a teardown, and deleting one deletes a customer's
data.

`destroy_buckets_with_contents` is true by default so a predeployment teardown
works. Set it false once these hold anything a customer would miss.

## What a teardown does not touch

- The five Google MX records, the SPF TXT, and the site-verification TXT on the
  zone. `deploy/cloudflare` owns two CNAMEs and nothing else, so a destroy there
  cannot take mail with it. Never clear the zone; delete records by id.
- The billing catalog and published rates. They are not in Terraform and have no
  un-publish, because a rate boundary is a figure customers were charged either
  side of.
- Anything in the account this platform did not create. That account also holds
  an EKS cluster and several SageMaker and DataZone environments.

## Rebuilding from nothing

Destroy and apply reproduces the deployment; that has been executed and 86
resources came back in one apply. Two things do not come back on their own:

- **Externally-sourced secrets.** A destroy removes the containers and their
  values, so the seven written in step 3 have to be written again. Terraform's own
  generated secrets return automatically.
- **The administrator credential.** If the host's volumes are destroyed while the
  database survives, bootstrap refuses with *"the committed request has no staged
  credential"* — the durable record says published and the file it published to is
  gone. It will not silently re-mint an administrator token. Reset the schema, or
  restore the credential.

## Two hosts

Not done, and the order matters:

1. Redis moves to ElastiCache. It is on the host today and holds the leases the
   scheduler serialises capacity work on; two hosts with two Redises is two
   schedulers that do not know about each other.
2. The instance takes a `count`. No load balancer is needed — the API advertises a
   Tailscale Service and the healthcheck refuses to advertise before it serves, so
   a second host is a second advertiser.
3. `public-ingress` runs on both. cloudflared supports several connectors per
   tunnel.

The cache server is the one service that cannot simply be duplicated: two
instances against one volume is two evictors on one store. Per host it is fine.
