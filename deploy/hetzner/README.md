# Hetzner platform capacity

The adapter uses dedicated x86 Cloud servers and the existing agent, gVisor,
WireGuard, storage, and metering paths. It does not support shared-CPU servers,
Hetzner Robot, attached volumes, or GPUs. GPU capacity remains on AWS.

Each server receives a unique, short-lived bootstrap token through its own
user-data. The control plane stores it encrypted and binds it to the server's
launch before redemption. The node generates its own credential locally before
the first request. Redemption consumes the bootstrap token once and binds that
node credential; successful reporting removes the local bootstrap token.
Neither a project API token nor a reusable join credential goes into user-data
or the image. Source IP is not an authenticator.

This change has no live Hetzner acceptance record. Do not activate it in
production until the disposable workflow below passes.

## Prepare a host image

Choose the project explicitly. Supply its token through `HCLOUD_TOKEN`, never
through arguments, committed files, or command output. Install Packer locally.
Select the immutable numeric ID of an Ubuntu 24.04 x86 base image in that
project's catalog.

Run from the repository root:

```sh
uv run python -m deploy.hetzner.bake --help
uv run python -m deploy.hetzner.bake \
  --base-image-id BASE_IMAGE_ID --location fsn1 \
  --manifest /absolute/path/to/new-bake-manifest.json
```

The build creates one paid CCX23 server and retains one paid snapshot. Packer
prints the temporary server and SSH key it creates and removes. Record their
IDs and the primary IP ID while the server exists. After the build, confirm
those exact resources are absent in the same project. A failed build is not
cleanup evidence. Delete only resources the run created and can name.

The image contains Docker, WireGuard tools, FUSE, and zram. It contains no
agent registration, tenant data, reusable SSH identity, or application release.
Its recipe digest covers the base image ID and preparation scripts. The agent
binary and worker image still come from the deployment's release manifest.

The Packer manifest records the snapshot ID and `recipe_sha256`. The snapshot
label `lazycloud-release` contains the first 63 digest characters because of
Hetzner's label limit. Set both the exact ID and full digest in each location's
`images_by_location` entry. Do not relabel an unrelated snapshot to pass this
check.

`--validate` checks template syntax only, without creating provider resources.
It does not prove the image boots or the runtime works.

## Configure capacity

Before applying the deployment's new secret map, add
`LAZYCLOUD_PLATFORM_CAPACITY_HETZNER` to the existing operator secret document,
preserving every other field. Its value is a JSON array. Use `[]` if no project
is configured; the ExternalSecret requires the named field to exist.

Each binding follows `provider_clients.settings.HetznerCapacityBinding`:

- `ref` is a stable name such as `hetzner:platform`. Renaming it while units
  exist would prevent their reconciliation and cleanup.
- `api_token` belongs to the selected project. Keep it in the secret document.
- `images_by_location` maps native locations to `image_id` and `recipe_sha256`.
- `allowed_server_types` contains only reviewed dedicated x86 SKUs.
- `usd_per_currency_unit` converts the project's API currency into USD.
- `primary_ipv4_hourly_micros` adds the reviewed IP charge to server offers.
- `policy` names the existing platform capacity workspace, pool, allowed and
  default native regions, instance limits, disk requirement, and cost ceilings.
  Set `platform_fleet` to true. Set `warm_cpu_min` to 1 only on the chosen
  default binding; every other binding uses 0. Bound `warm_cpu_max` and
  `max_cpu_instances` to the approved spend.

Policy ceilings use exact capability keys, for example
`hetzner:fsn1:ccx33:amd64:runsc`, in USD microdollars per server-hour. They are
supplier purchase limits, not retail rates or margin guarantees. Budget for
idle time, image transfers, object-store access, IPs, and fees separately.
The requested root volume must fit the SKU's included disk.

AWS platform capacity also requires reviewed ceilings. Set Terraform's
`platform_aws_hourly_cost_ceiling_micros`, keyed like
`aws:us-east-1:g6.xlarge:amd64:runsc`, before deployment. A missing capability
cannot buy or restore machines. Customer-connected AWS is unaffected. Do not
derive these ceilings blindly from supplier prices: that would approve any
price without checking the revenue it can earn.

Both API and scheduler must receive the same settings and release manifest.
Apply the forward launch-credential migration before starting either process.
The node's root-only state directory holds its launch ID, bootstrap token, and
locally generated node credential. Tenant containers cannot read that directory
or reach provider metadata through their network namespace.

## Disposable acceptance and rollout

Use an approved non-production target with reachable object storage, public
bootstrap origin, private callback routes, and the release's real agent binary.
Start with one allowed SKU, one default location, a one-node maximum, and zero
warm nodes. Name every created unit, server, IP, enrollment, worker, and image
in the acceptance record.

Submit a CPU function through the public SDK, then poll the durable unit and
enrollment, provider server, bootstrap log, scheduler request, and worker log.
Prove the returned function result and usage charge. Confirm a second compatible
request shares existing or pending capacity rather than buying another node.

Run separate checks for expired or replayed bootstrap tokens, wrong node
credentials and server IDs, restart reconciliation after an uncertain create,
idle deletion near the paid-hour boundary, and deletion after failed bootstrap.
Prove that deleting the exact server does not recreate it. Confirm the server
and primary IP are absent and no volume was attached. Preserve shared images
and all unrelated project resources.

Only then enable the one-node warm floor. Regional selection remains unavailable
until explicit regional compute rates are reviewed and published. Omitting a
region uses Auto pricing regardless of the supplier that runs the container.

For rollback, first lower the warm floor and stop new acquisitions with the
binding's limits. Let active work finish and let the normal drain delete idle
nodes. Keep the binding, token, images, and new application version until all
units prove cleanup. Removing credentials first strands paid capacity.

Forward database migrations preserve deployed state. Do not reset a deployed
database or downgrade the schema as a rollback procedure.
