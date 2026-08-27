# Tailnet deployment

This Terraform configuration owns the complete policy and runtime credentials
for one LazyCloud-managed Tailnet per state. Production and development apply
this same configuration with different Tailnet IDs, management credentials,
state keys, and Terraform data directories. It is appropriate only when the
reviewed plan proves that whole-policy ownership is intended.

For live testing, whichever Tailnet the user selects or supplies is approved,
regardless of whether its account name appears personal or shared. A provided
Tailnet must be treated as shared external state: inspect it before mutation,
preserve all unrelated users, devices, tags, grants, ACL rules, DNS and route
configuration, OAuth clients, and keys, and create or remove only exact
LazyCloud-owned test resources. Do not apply this whole-policy Terraform module
to such a Tailnet unless the user explicitly assigns the entire policy to this
module and a reviewed plan proves unrelated state is preserved.

It creates a deny-by-default policy, a scoped OAuth client, and the tailnet-wide
DNS and device settings this deployment depends on. The hosted Tailnet already
must exist.

Production uses the existing dedicated Tailnet. Local development uses an
API-only Tailnet named `LazyCloud Development`. API-only Tailnets contain only
tagged devices and do not appear in the Tailscale admin console.

Nothing here mints a long-lived enrollment key. The control plane issues its own
short-lived, tagged key through the OAuth client when it joins, so no credential
sits in a deployment file waiting to be leaked or to expire unnoticed.

Two tags carry two different reaches. `control_plane_tag` may dial an agent's
route proxy; `agent_tag` may dial the control plane. A node that has not
enrolled yet holds no tailnet identity at all: it reaches the control plane over
the public origin and joins the tailnet with the machine key enrolment vends it.

The OAuth client owns both tags. It mints `agent_tag` keys for every node it
enrols and `control_plane_tag` keys for the device the control plane registers as
itself — `tailscale up` advertises no tag, so for the control plane this client is
the only thing that can apply one. Changing that tag list replaces the client, so
re-export `agent_oauth_client_id` and `agent_oauth_client_secret` to the
deployment secret manager after any plan that does.

Both tags therefore list `control_plane_tag` among their owners. An OAuth client
authenticates as its tags rather than as a user, so a tag owned only by
`autogroup:admin` is a tag the client cannot apply: every mint fails with
"requested tags are invalid or not permitted", and the deployment enrols nothing.
Self-ownership on `control_plane_tag` is what lets each control plane mint the key
for its own device, which is what makes a second replica a start rather than an
enrolment step.

The control plane advertises `control_plane_service` and is reached there rather
than at any one device's name. `autoApprovers` lets a node carrying
`control_plane_tag` become a service proxy without an admin approving each one,
which is what allows a second control plane to be added by starting it.

The service itself is a resource here because it must exist before any node may
advertise it, and advertising an absent one fails silently: the node sets the
preference, the coordination server reads it back, and the name still resolves
nowhere while every process reports healthy. `autoApprovers` approves a proxy for
a service; it does not create the service.

Its ports are the deployment's, not a wish list. Tailscale hands the service
address to consumers only once a host serves every port the service declares, so
one extra port withholds the whole service. That is why `tcp_ingress_enabled`
exists and why it has to say the same thing as `LAZYCLOUD_TCP_INGRESS_ENABLED`:
a service declaring `tcp:9000` and `tcp:1995` against a control plane serving
only 9000 is advertised, granted, and resolvable, and no peer ever receives the
address. Both ends report healthy, every node enrols, and no worker ever
registers. The console says "advertising the service, but some required ports
are missing"; nothing else does. The control plane logs the ports it serves next
to the origin it publishes, which is the other half of that comparison.

## Development Tailnet bootstrap

Tailscale's API-only Tailnet create and delete endpoints are alpha and are not
available in the Terraform provider. `bootstrap_api_only.py` keeps that one-time
creation outside Terraform instead of hiding it in a `local-exec` resource.
Terraform manages the Tailnet after it exists.

Create a temporary OAuth client in the existing Tailscale organization with the
`tailnets` scope. Export it only for the bootstrap process:

```sh
export TAILSCALE_BOOTSTRAP_OAUTH_CLIENT_ID=<temporary-client-id>
export TAILSCALE_BOOTSTRAP_OAUTH_CLIENT_SECRET=<temporary-client-secret>
uv run python deploy/tailnet/bootstrap_api_only.py
```

The command lists API-only Tailnets before creating anything. It refuses a
duplicate display name and never prints a secret. On success it writes the new
Tailnet's `all`-scope Terraform client to
`~/.config/lazycloud/tailnet-development-terraform.env` with mode `0600`.
Revoke the temporary bootstrap client after creation. Keep the development
management client for future Terraform plans, and never copy it into `.env` or
a workload secret.

If the Tailnet exists but the credential file is missing, the command stops.
It cannot recover a secret that Tailscale returned only during creation.

## Backend and credentials

Terraform state contains secrets. Supply a standard remote backend owned by the
deployment operator; this repository deliberately does not create or delete its
bucket. The checked-in `backend "s3" {}` declaration accepts normal
`terraform init -backend-config=...` values or a reviewed backend config file.
Keep backend coordinates and credentials outside the repository. Production
keeps its existing backend key. Development uses
`tailnet/development.tfstate`. Never reinitialize one environment onto the
other environment's key.

Give each environment a separate `TF_DATA_DIR` so cached backend metadata
cannot point a development command at production. For development, source the
file written by the bootstrap command. It exports the Tailnet ID for both the
provider and the required Terraform variable:

```sh
export AWS_PROFILE=platform-operations
export TAILNET_ENVIRONMENT=development
export TAILNET_STATE_BUCKET=<state-bucket>
export TAILNET_STATE_KEY=tailnet/development.tfstate
export TAILNET_STATE_REGION=us-east-1
export TAILNET_VARIABLES=development.tfvars.example
export TF_DATA_DIR="$PWD/deploy/tailnet/.terraform/$TAILNET_ENVIRONMENT"
source "${XDG_CONFIG_HOME:-$HOME/.config}/lazycloud/tailnet-development-terraform.env"

terraform -chdir=deploy/tailnet init -reconfigure \
  -backend-config="bucket=$TAILNET_STATE_BUCKET" \
  -backend-config="key=$TAILNET_STATE_KEY" \
  -backend-config="region=$TAILNET_STATE_REGION" \
  -backend-config="encrypt=true" \
  -backend-config="use_lockfile=true"
```

For production, set `TAILNET_ENVIRONMENT=production`, use the existing
production backend key and management credential, and set
`TAILNET_VARIABLES=production.tfvars.example`. Export both `TAILSCALE_TAILNET`
and `TF_VAR_tailnet_id` with the production Tailnet ID. The provider accepts
either `TAILSCALE_API_KEY` or its OAuth client environment variables. Do not put
credentials in `*.tfvars`.

The operator owns backend creation, encryption, versioning, access policy,
retention, and final deletion through the organization-standard infrastructure
workflow.

## Plan and apply

Review the environment's example variables and the whole-policy ownership
confirmations. Then run independent reviewed actions:

```sh
terraform -chdir=deploy/tailnet fmt -check
terraform -chdir=deploy/tailnet validate
terraform -chdir=deploy/tailnet plan \
  -var-file="$TAILNET_VARIABLES" \
  -out="$TF_DATA_DIR/tailnet.tfplan"
terraform -chdir=deploy/tailnet apply "$TF_DATA_DIR/tailnet.tfplan"
```

Do not publish a saved plan. Move sensitive outputs directly from encrypted
state into the deployment secret manager:

| Terraform output | Deployment setting |
| --- | --- |
| `agent_oauth_client_id` | `LAZYCLOUD_TAILNET_OAUTH_CLIENT_ID` |
| `agent_oauth_client_secret` | `LAZYCLOUD_TAILNET_OAUTH_CLIENT_SECRET` |

`runtime_configuration` carries both tag names, which must match the
deployment's `LAZYCLOUD_TAILNET_AGENT_TAG` and
`LAZYCLOUD_TAILNET_CONTROL_PLANE_TAG`. A tag the policy does not grant produces
nodes that join the tailnet and cannot reach anything.

`tailnet_id` names the exact Tailnet owned by the selected state. Check it
before transferring runtime credentials. Development outputs go only to local
`.env`; production outputs go only to the production operator secret.

Generate `LAZYCLOUD_BACKEND_ROUTE_AUTH_KEY` separately in the application
secret manager.

## Teardown

Destroy Tailnet resources with a saved, reviewed plan:

```sh
terraform -chdir=deploy/tailnet plan \
  -destroy \
  -var-file="$TAILNET_VARIABLES" \
  -out="$TF_DATA_DIR/tailnet-destroy.tfplan"
terraform -chdir=deploy/tailnet apply "$TF_DATA_DIR/tailnet-destroy.tfplan"
terraform -chdir=deploy/tailnet state list
```

An empty state proves this module no longer owns Tailnet resources. Backend
retention or deletion is a separate operator-owned action. Destroying the
Terraform resources does not delete an API-only Tailnet. That separate API
deletion is irreversible and requires explicit approval for the exact Tailnet
ID printed by the `tailnet_id` output.

## Acceptance handoff

Acceptance consumes this already-applied deployment. The operator exports the
public LazyCloud endpoint and tokens, the exact prepared Tailscale socket, and
the guarded node/pool/worker/machine identities. Run only the matching exact
scenario under `tests/e2e/external/tailnet/`.

Scenarios never run Terraform, build or restart Compose, mint Tailnet
credentials, create or delete devices/pools, or discard agent state. Agent
restart is a separate deliberate operator action; `restart_continuity.py`
verifies the resulting stable public identities and route. Destructive
Terraform teardown remains the reviewed action above.
