# Hetzner worker images and credentials

Use the shared [provider provisioning guide](../PROVIDERS.md) for deployment,
scaling, verification, and cleanup. This file covers Hetzner's inputs and image
build. It is not a separate application deployment procedure.

Live workload acceptance is outstanding. The owner approved performing it on
production with the intended warm/cold configuration after release checks pass.

## Project credentials

Create a read/write Cloud API token for the selected project. The deployment's
operator secret contains `LAZYCLOUD_PLATFORM_CAPACITY_HETZNER_TOKENS`, a JSON
object mapping `hetzner:platform` to that token. Preserve every other field when
updating the existing secret. No token belongs in Terraform variables, state,
image manifests, user-data, or source control.

The GitHub `release` environment needs `HCLOUD_TOKEN` for image builds. It is
the same selected project, but a separate secret consumer. Storing the token
locally does not publish either secret automatically.

## Host image build

Select an immutable numeric Ubuntu 24.04 x86 base-image ID from the project's
catalog. Run the image workflow from main:

```sh
gh workflow run hetzner-node-images.yml --ref main \
  -f base_image_id=BASE_IMAGE_ID -f location=ash
```

Download its `hetzner-node-image-<run-id>` artifact. It contains the Packer
manifest and `hetzner-images.tfvars.json`. The latter supplies verified image
IDs and recipe digests to the normal Terraform deployment:

```sh
terraform -chdir=deploy/platform-deployment plan \
  -var-file=/absolute/path/to/hetzner-images.tfvars.json
```

Use the deployment's existing backend and variable files. Review and apply
that plan before Ship, which reads Terraform's published infrastructure descriptor.

A local build uses the same Packer recipe. Supply `HCLOUD_TOKEN` through the
environment, then run:

```sh
uv run python -m deploy.hetzner.bake \
  --base-image-id BASE_IMAGE_ID --location ash \
  --manifest /absolute/path/to/new-image-manifest.json
uv run python -m deploy.hetzner.catalog \
  --manifest /absolute/path/to/new-image-manifest.json \
  --output /absolute/path/to/hetzner-images.tfvars.json
```

The catalog command checks the manifest recipe against this checkout and the
snapshot against the real project. It refuses an unfinished build server and
never creates provider resources or writes credentials. Its output must be a
new file. For additional locations, preserve existing image entries when
assembling the deployment's image map.

The build creates one paid CCX23 server and retains one paid snapshot. Record
the exact server, primary IP, and temporary SSH key IDs during the build, then
verify their absence. The catalog check does not replace the IP and SSH-key
cleanup audit. A failed build is not cleanup evidence. Never remove unrelated
project resources or a shared image still used by a deployment.

The image contains Docker, gVisor dependencies, WireGuard, FUSE, and compressed
swap. It contains no tenant data or reusable enrollment credential. Application
releases supply the common agent and worker image separately. Rebuild the host
image when its recipe changes, not on every application release.

`bake --validate` checks template syntax without creating resources. It does
not prove that a node can enroll or run a workload.

## Deployment defaults and differences

`deploy/chart/environments/prod.yaml` declares Ashburn, `ash`, with CCX33,
CCX43, CCX53, and CCX63. The cheapest compatible CPU shape holds an adaptive
warm floor starting at one node. Larger shapes are acquired on demand. Other
platform pools remain cold. Users request container CPU, memory, and GPUs;
they do not select a server size.

The default shape range has enough included disk for the 200-GiB node policy.
The adapter supports dedicated x86 Cloud servers, not shared CPU, Robot,
attached volumes, or GPUs. AWS supplies GPU capacity. Additional locations
need their images and deployment policy before they can supply capacity.

Server prices come from the provider API. Helm declares the USD conversion
and IPv4 cost added to those prices. The defaults match the selected USD-billed
project; review them for another billing currency or changed supplier charges.
There are no added supplier purchase ceilings or Hetzner node caps.

Hetzner has no AWS-style instance identity proof. Each launch therefore receives
a unique, short-lived bootstrap token in that node's user-data, as approved by
the owner. The control plane binds it to the launch and consumes it once; the
node generates its own continuing credential. Project API tokens never reach
workers. The common agent owns runtime installation, reporting, and execution.
