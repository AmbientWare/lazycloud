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

Live workload acceptance is outstanding. The owner approved performing it on
production with the intended warm/cold configuration. Merge and deployment
remain on hold until the owner releases the hold for the other feature.

## Deployment flow

Hetzner uses the same release split as AWS. Prepare the host image once with
the `Hetzner Node Images` workflow, configure the project once in the deployment's
operator secret, then use the existing Ship workflow for application releases.
Ship does not rebuild host images or need another provider-specific deploy.

The initial location is Ashburn, `ash`. Keep other locations disabled until
their images, costs and acceptance are reviewed. The Ashburn catalog includes
CCX33, CCX43, CCX53, and CCX63, all with enough included disk for the current
200-GiB node policy. The CCX23 build server is only temporary.

Before the first image build, an operator must place the selected project's
token in the GitHub `release` environment secret `HCLOUD_TOKEN`. The local token
file is not uploaded automatically. Dispatch from `main`:

```sh
gh workflow run hetzner-node-images.yml --ref main \
  -f base_image_id=BASE_IMAGE_ID -f location=ash
```

The workflow uploads a credential-free `hetzner-node-image-<run-id>` artifact.
Download its `hetzner-node-image.json` manifest for the configuration command
below. A successful image build does not prove that a workload can run.

## Prepare a host image

Choose the project explicitly. Supply its token through `HCLOUD_TOKEN`, never
through arguments, committed files, or command output. Install Packer locally.
Select the immutable numeric ID of an Ubuntu 24.04 x86 base image in that
project's catalog.

Run from the repository root:

```sh
uv run python -m deploy.hetzner.bake --help
uv run python -m deploy.hetzner.bake \
  --base-image-id BASE_IMAGE_ID --location ash \
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

`configure.py` prepares the operator document without editing JSON by hand.
First export the current deployment operator document using an approved AWS
operator identity into an owner-only file outside the repository. Do not start
from an empty document on an existing deployment. The command preserves other
operator fields and provider bindings, and refuses to replace a different
policy already using the same binding ref.

Choose a private output directory with mode `0700`; both the token file and
exported operator document must be owner-only. Then run:

```sh
uv run python -m deploy.hetzner.configure \
  --manifest /absolute/path/to/hetzner-node-image.json \
  --token-file /absolute/path/to/hetzner-token \
  --operator-document /private/path/operator-current.json \
  --output /private/path/operator-with-hetzner.json \
  --workspace-id PLATFORM_CAPACITY_WORKSPACE_ID \
  --server-type ccx33 --server-type ccx43 \
  --server-type ccx53 --server-type ccx63 --warm-cpu-min 1
```

The command checks the manifest against the current host recipe and the
snapshot in the token's actual project. It checks disk size and reports the
current supplier price including IPv4. It writes a new `0600` document
outside the repository, never credentials to stdout. The initial binding uses
only the manifest's location and a one-node warm floor by default. Use
`--warm-cpu-min 0` for any additional cold binding. Repeating the same setup
preserves an identical binding; changing an active binding needs explicit review.
USD and IPv4 prices come from the project's API. A project billed in another
currency requires an explicit reviewed `--usd-per-currency-unit` conversion.

This prepares a local document only. The operator must publish it to the
existing `<deployment>/operator` secret before deployment, checking that no
other operator changed the source document meanwhile. Do not upload this
credential-bearing file as a GitHub artifact. Complete build-resource cleanup
before deployment. Activating this configuration starts billable warm capacity.

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
  default native regions, disk requirement, and idle behavior.
  Set `platform_fleet` to true. Set `warm_cpu_min` to 1 only on the chosen
  default binding; every other binding uses 0. The warm target follows measured
  arrivals without a separate provider maximum.

The warm controller holds the cheapest compatible CPU shape in the default
region. It counts only requests that fit that shape. Larger shapes start cold
and are acquired when a request needs them. Offer selection checks requested
CPU and memory with host headroom, then chooses the cheapest compatible node.
Users request container resources, not server sizes. Several containers may
share one node. This is per-request sizing, not a fleet-wide batch optimizer.

Account admission enforces plan concurrency and billing limits. Hetzner adds no
node-count or supplier-price ceiling. Existing AWS connection limits remain.
The scheduler ranks compatible offers by cost, but does not guarantee a margin
or stop scaling when supplier prices rise. Budget for idle time, image transfers,
object-store access, IPs, and fees separately. The requested root volume must
fit the SKU's included disk.

Both API and scheduler must receive the same settings and release manifest.
Apply the forward launch-credential migration before starting either process.
The node's root-only state directory holds its launch ID, bootstrap token, and
locally generated node credential. Tenant containers cannot read that directory
or reach provider metadata through their network namespace.

## Production acceptance and rollout

After the owner releases the hold, integrate current main, pass CI, and merge.
Preserve production state and use its object storage, public bootstrap origin,
private callback routes, and the release's real agent binary. Start with the
Ashburn size range above and one warm CPU node. Other platform pools have no
warm floor. Name every created unit, server, IP, enrollment, worker, and image
in the acceptance record.

Submit a CPU function through the public SDK, then poll the durable unit and
enrollment, provider server, bootstrap log, scheduler request, and worker log.
Prove the returned function result and usage charge. Confirm a second compatible
request shares existing or pending capacity rather than buying another node.
Submit larger CPU and memory requests that cannot fit the warm shape, and prove
that placement acquires a suitable larger node. Check AWS GPU placement too.

Run separate checks for expired or replayed bootstrap tokens, wrong node
credentials and server IDs, restart reconciliation after an uncertain create,
idle deletion near the paid-hour boundary, and deletion after failed bootstrap.
Prove that drained cold nodes are not recreated without demand, and confirm
their exact servers and primary IPs are absent. No volume should be attached.
Retain the intended warm baseline; a warm node's replacement is expected.
Preserve shared images and all unrelated project resources.

Regional selection remains unavailable
until explicit regional compute rates are reviewed and published. Omitting a
region uses Auto pricing regardless of the supplier that runs the container.

Rollback needs a maintenance window that stops new platform workload admission.
Lower the warm floor, let active work finish, and let the normal drain delete idle
nodes. There is no provider purchase-limit switch. Keep the binding, token,
images, and new application version until all
units prove cleanup. Removing credentials first strands paid capacity.

Forward database migrations preserve deployed state. Do not reset a deployed
database or downgrade the schema as a rollback procedure.
