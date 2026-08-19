# Platform AWS

The AWS infrastructure this platform runs on: the control plane host, its
identity, the registries and buckets it reads, and the secret containers its
credentials live in.

The split across `deploy/` is by ownership, not by provider. Terraform owns
everything we operate — this module, `cloudflare`, `stripe`, `tailnet`.
CloudFormation owns only what runs inside an account we do not have credentials
for: `connected-aws/customer_stack.py` and the template the control plane
generates for it.

## What this module does not own, and why

**The shared fleet's network.** The VPC, its two subnets, the security group and
the node instance profile are created by the account connection stack, and its
outputs are what a pool launches into. The platform account connects to itself
through the same managed flow a customer uses, which is what
`connected-aws/control-stack.yaml` already anticipates: *"a customer account can
be this account."*

Declaring that network here would mean declaring the connection role beside it,
and that role's policy is generated in `provider_aws/account_connection.py`
against CloudFormation refs. A hand-written copy would drift from its owner the
first time the generator changed, and the drift would show up as a launch that
fails in a way the policy no longer explains.

**`connected-aws/control-stack.yaml`.** The control principal stays
CloudFormation. Its `ControlRole` carries `DeletionPolicy: Retain` because AWS
rewrites a role-ARN principal in a customer's trust policy to that role's unique
ID: delete the role and recreating the same name does not restore the trust, and
every live connection needs a new authorization generation. A resource whose
defining property is that it must outlive its own manager is a poor fit for a
tool that converges to declared state. It is provisioned once and essentially
never changes.

**The database.** PlanetScale's Terraform provider is MySQL-only. At 0.6.1 there
is no resource that creates a PlanetScale Postgres database, and
`planetscale_database` has no engine selector. See `database.tf`, and the manual
step below.

**Secret values.** This module declares the containers and who may read them.
Every value is written by `lazycloud-admin bootstrap publish` or by an operator.
A secret whose value is in Terraform is a secret in the state file.

## Bring-up

```sh
terraform -chdir=deploy/platform-aws init \
  -backend-config="bucket=<state-bucket>" \
  -backend-config="key=platform-aws/terraform.tfstate" \
  -backend-config="region=us-east-1"
terraform -chdir=deploy/platform-aws apply
```

Then, in order:

1. **Create the Postgres database** in the PlanetScale console. It must permit
   `CREATE EXTENSION btree_gist` and `pgcrypto`; both are supported and neither
   needs a restart or superuser.

2. **Write `database-url`** into Secrets Manager, using the **direct endpoint on
   5432**, not the PgBouncer one. `database.tf` explains what breaks otherwise,
   and it breaks silently.

   ```sh
   aws secretsmanager put-secret-value \
     --secret-id "$(terraform -chdir=deploy/platform-aws output -raw deployment)/database-url" \
     --secret-string 'postgresql+psycopg://...:5432/...'
   ```

3. **Trust the control plane role** in the connected-AWS control stack, so the
   control plane can assume it:

   ```sh
   uv run python deploy/connected-aws/bootstrap.py \
     --trusted-principal "$(terraform -chdir=deploy/platform-aws output -raw control_plane_role_arn)"
   ```

4. **Publish the release**, which builds every source-bearing image in one
   invocation and writes the deploy bundle. Do not perform the steps by hand;
   `deploy/release.py` exists because the documented sequence was still performed
   wrong, and a release assembled from two source states is rejected at container
   start as a package-digest mismatch that names a digest rather than the stale
   artifact.

5. **Bootstrap the deployment** and connect the platform account to itself. See
   `deploy/RUNBOOK.md`.

## Redeploying

Nothing changes what this host runs except the bundle in the deploy bucket.

```sh
aws ssm send-command \
  --document-name AWS-RunShellScript \
  --instance-ids "$(terraform -chdir=deploy/platform-aws output -raw control_plane_instance_id)" \
  --parameters 'commands=["/usr/local/bin/lazycloud-deploy"]'
```

No SSH key exists and no inbound rule is open. Operator access is SSM Session
Manager, which the host dials outbound.

## One host

The API is replica-safe and advertises a Tailscale Service, so a second host is a
second advertiser rather than a load balancer. Redis is the thing that has to
move to ElastiCache first: it lives on this host, and it holds the leases the
scheduler serialises capacity work on.
