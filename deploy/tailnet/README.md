# Tailnet Deployment

This Terraform configuration owns the complete policy and runtime credentials
for one LazyCloud-managed Tailnet. It is appropriate only when the reviewed
plan proves that whole-policy ownership is intended.

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

## Backend and credentials

Terraform state contains secrets. Supply a standard remote backend owned by the
deployment operator; this repository deliberately does not create or delete its
bucket. The checked-in `backend "s3" {}` declaration accepts normal
`terraform init -backend-config=...` values or a reviewed backend config file.
Keep backend coordinates and credentials outside the repository.

Authenticate AWS and Tailscale with short-lived operator credentials. Do not
put credentials in `*.tfvars`:

```sh
export AWS_PROFILE=platform-operations
export TAILSCALE_TAILNET=your-dedicated-tailnet-id
export TAILSCALE_API_KEY=temporary-admin-api-key

terraform -chdir=deploy/tailnet init \
  -backend-config="bucket=$TAILNET_STATE_BUCKET" \
  -backend-config="key=$TAILNET_STATE_KEY" \
  -backend-config="region=$TAILNET_STATE_REGION" \
  -backend-config="encrypt=true" \
  -backend-config="use_lockfile=true"
```

The operator owns backend creation, encryption, versioning, access policy,
retention, and final deletion through the organization-standard infrastructure
workflow.

## Plan and apply

Copy `terraform.tfvars.example` into a secure operator directory and review the
whole-policy ownership confirmations. Then run independent reviewed actions:

```sh
terraform -chdir=deploy/tailnet fmt -check
terraform -chdir=deploy/tailnet validate
terraform -chdir=deploy/tailnet plan -out=tailnet.tfplan
terraform -chdir=deploy/tailnet apply tailnet.tfplan
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

Generate `LAZYCLOUD_BACKEND_ROUTE_AUTH_KEY` separately in the application
secret manager.

## Teardown

Destroy Tailnet resources with a saved, reviewed plan:

```sh
terraform -chdir=deploy/tailnet plan -destroy -out=tailnet-destroy.tfplan
terraform -chdir=deploy/tailnet apply tailnet-destroy.tfplan
terraform -chdir=deploy/tailnet state list
```

An empty state proves this module no longer owns Tailnet resources. Backend
retention or deletion is a separate operator-owned action.

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
