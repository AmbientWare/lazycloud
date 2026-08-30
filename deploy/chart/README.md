# LazyCloud chart

This chart runs the control plane, scheduler, cache, bootstrap jobs, platform
Newt connector, and the Pangolin CLI client sidecars.

The deployment workflow renders values from Terraform outputs. `runtime` holds
non-secret settings. External Secrets materializes separate shared, Pangolin
control, and Pangolin runtime Secrets so generated connector credentials do not
reach unrelated workloads.

## Pangolin connections

`pangolin-site` is an ordinal set of single-replica Newt Deployments, with two
members by default. Each Deployment projects only its own Pangolin site
credential and connects that site to the stable `control-plane:9000` Service.

Each control-plane ordinal is also a single-replica Deployment with a
`pangolin-cli` sidecar. Containers in a Pod share a network namespace, so the API
can dial private agent resources through the sidecar's WireGuard interface. The
sidecar projects only its ordinal's machine-client credential and is the only
container that receives it or `NET_ADMIN`.

Terraform sizes both identity sets from `pangolin_site_replicas` and
`control_plane_replicas`. The values renderer derives the StatefulSet counts
from those generated credential keys, so increasing either variable creates
the missing Pangolin identities before the new ordinals start.

The Newt and client probes check Pangolin connectivity. A Pod does not become
Ready merely because the application process is listening.

## Bootstrap order

Argo sync waves create the shared and Pangolin control Secrets, run the Pangolin
bootstrap, then materialize separate provider, site, and client Secrets from its
generated runtime document. The schema precedes administrator bootstrap and the
rate card. Each database job runs alone so the connection-budget check remains
true during a rollout.

Write `LAZYCLOUD_TOKEN` to the operator secret before the first sync. Bootstrap
adopts that token. If it runs without one, it creates a token that exists only
inside the completed Job and the predeployment database must be reset.

`billing.ratesEffectiveAt` stays in `values.yaml` because it is a customer
charge boundary, not infrastructure data. Change it in the same commit as the
shared rate card.
