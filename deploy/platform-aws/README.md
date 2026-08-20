# Platform AWS

Everything this platform runs on, declared. The control plane host and its
identity, the control principal, the shared fleet's network and connection role,
the registries and buckets, and the containers the credentials live in.

## Two deployment models

**Ours.** This module. Declared, repeatable, and overwritten on apply. There is
nothing to import and nothing to adopt: an apply against an empty account
produces the whole deployment, and an apply against an existing one converges it.

**A customer's.** `deploy/connected-aws` — a CloudFormation template the customer
deploys in their own account, from a console link, with no credentials shared and
no tooling required on their side. It is CloudFormation for exactly one reason:
we hold no credentials for that account, so we cannot run Terraform there.

The line is ownership, not provider. Nothing about our account is CloudFormation
any more.

## One definition of the permission set

Three consumers need the same list of what the control plane may do inside a
connected account: the customer's template, our own connection role, and the
document a customer needs when bringing their own role.

`provider_aws/connection_policy.py` owns it. The customer template renders it at
request time. Terraform cannot run Python, so `connection-role-policy.json` is
rendered by `deploy/render_connection_policy.py` and committed, and CI runs that
script with `--check` so a stale file fails the build.

That check is not ceremony. A second copy of a permission set drifts into a role
missing an action the control plane started calling, and that surfaces as a
launch denial naming an API call rather than the policy behind it.

## What this module still does not own

**Secret values.** It declares the containers and who may read them. Values are
written by an operator or by bootstrap. A secret whose value is in Terraform is a
secret in the state file. The one exception is the fleet connection's external
ID, which both sides must agree on and nothing else can create consistently;
`fleet.tf` says why that is acceptable.

There is no second exception for the database. Terraform creates the branch and
the role it connects as, so that role's password is in state. State lives in an
encrypted bucket and should be treated as holding a database credential, because
it does.

## Bring-up

`LIFECYCLE.md` covers standing a deployment up and taking one down in full,
including what a teardown cannot remove on its own. This is the short form.


```sh
DEPLOYMENT=lazycloud-prod
terraform -chdir=deploy/platform-aws init \
  -backend-config="bucket=<state-bucket>" \
  -backend-config="key=platform-aws/$DEPLOYMENT.tfstate" \
  -backend-config="region=us-east-1"
terraform -chdir=deploy/platform-aws apply -var="deployment=$DEPLOYMENT"
```

The state key carries the deployment name. Two deployments sharing one key share
one state, and the second apply destroys the first.

## Two deployments in one AWS account

Supported. Every globally-named resource carries `var.deployment`: buckets, ECR
repositories, secret paths, IAM roles, and the workspace bucket prefix that
scopes the control plane's S3 grant.

Three values need attention:

- `control_role_name` is the one name without the prefix, because a customer's
  trust policy embeds it. Give a non-production deployment a different value; the
  contract only binds where customers already connected.
- The Tailscale hostname carries the deployment name, from `runtime_configuration`.
  A tailnet is shared across AWS accounts, so this would collide even if the
  accounts did not.
- `deploy/cloudflare` and `deploy/stripe` are separate modules with their own
  state. A second deployment needs its own tunnel, its own hostnames, and test
  mode for billing.

The S3 bucket quota is per-account, defaults to 10,000, and every workspace
consumes one. Two deployments share that ceiling.

Every remaining secret has a container but no value. Write the ones the
deployment needs — the GitHub App pair, Stripe, Cloudflare, Tailscale, telemetry
— then:

1. **Publish a release.** `deploy/release.py` builds every source-bearing image in
   one invocation, then `deploy/bundle.py` publishes what the host converges onto.
   Do not perform the steps by hand: a release assembled from two source states is
   rejected at container start as a package-digest mismatch naming a digest rather
   than the stale artifact.

2. **Register the platform account's own capacity.** The fleet is a connection in
   existing-role mode, using `fleet_connection_role_arn`, the `fleet-external-id`
   secret, and the `fleet_network` output. That output is exactly two subnets in
   two zones, which is what `AwsAccountNetwork` accepts.

## Redeploying

Nothing changes what the host runs except the bundle in the deploy bucket.

```sh
aws ssm send-command \
  --document-name AWS-RunShellScript \
  --instance-ids "$(terraform -chdir=deploy/platform-aws output -raw control_plane_instance_id)" \
  --parameters 'commands=["/usr/local/bin/lazycloud-deploy"]'
```

No SSH key exists and no inbound rule is open. Operator access is SSM Session
Manager, which the host dials outbound.

## The control principal's name

`control_role_name` is pinned and must stay pinned. A customer's authorization
template writes this role's ARN into every connection role's trust policy, so the
name is a durable external contract. `prevent_destroy` guards the role for the
same reason: AWS rewrites a role-ARN principal to the role's unique ID, so
recreating the same name does not restore trust that already exists.

That guard matters once customers exist. Before then an apply is free.

## One host

The API is replica-safe and advertises a Tailscale Service, so a second host is a
second advertiser rather than a load balancer. Redis has to move to ElastiCache
first: it lives on this host and holds the leases the scheduler serialises
capacity work on.
