# Hyperstack GPU host images

The baker creates one paid `n3-A100-SXM4x8` VM in US-1 and publishes a private
Ubuntu 24.04 GPU host image. The existing operator token map
`LAZYCLOUD_PLATFORM_CAPACITY_HYPERSTACK_TOKENS` supplies its API key.

Select an existing US-1 environment and an immutable Ubuntu 24.04 image.
Hyperstack requires credit covering at least one hour of the account's combined
usage before VM creation. Check the current quote and leave enough credit for
the bake and later workload acceptance.

Check the exact assembled host script without creating resources:

```sh
uv run python -m deploy.hyperstack.bake \
  --manifest /tmp/hyperstack-image.json --validate
```

Build from the repository root. Replace the example address with the operator's
public IPv4 address; only that /32 can reach the builder's SSH port. Keep the
manifest outside the repository.

```sh
uv run python -m deploy.hyperstack.bake \
  --env-file ~/.lazycloud/operator/deploy.env \
  --manifest ~/.lazycloud/operator/hyperstack-image.json \
  --ssh-source-cidr 203.0.113.10/32
```

The command imports a temporary SSH public key, pins a separate SSH host key,
installs the canonical runtime prerequisites, then runs
`prepare-gpu.sh` and the shared Ubuntu finalizer. NVIDIA 590.48.01 matches the
production AMI driver pin and the gVisor ABI list. Container Toolkit is pinned
to 1.20.0-1. The GPU check uses the repository's pinned gVisor release and an
immutable CUDA sample image. A failing GPU check prevents publication.
Each of the eight GPU UUIDs runs the sample separately through gVisor.

The baker uses `POST /core/virtual-machines/{id}/snapshots`, polls the snapshot,
then calls `POST /core/snapshots/{id}/image`. It prints the deployment entry
containing `environment_name`, `keypair_name`, `image_name` and `recipe_sha256`
after publication and builder cleanup. Add that entry to the Hyperstack
binding in the deployment's existing platform-capacity configuration.

Successful builds retain exactly the published private image, its backing
snapshot and the required public keypair. The manifest records their IDs and
key fingerprint. The VM and local private SSH keys are deleted. The image
contains no agent state, enrollment tokens, authorized keys or SSH host keys.
Deleting a backing snapshot also deletes its linked image; retain it while the
deployment uses that image.

Failed builds remove their VM, unpublished image, snapshot and keypair. A
transport failure can leave a create outcome unknown. The baker retains its
manifest and refuses another purchase; it never treats an empty list as proof
that an attempted create did not happen. Reconcile cleanup using the same
manifest after provider state becomes visible:

```sh
uv run python -m deploy.hyperstack.bake \
  --env-file ~/.lazycloud/operator/deploy.env \
  --manifest ~/.lazycloud/operator/hyperstack-image.json --cleanup
```

An interrupted process may need this command too. Keep an unresolved manifest;
do not start another bake to bypass an unknown create. Failed, fully cleaned
builds need a new manifest. Concurrent operations on one manifest are refused.

Before enabling the provider, complete a funded build, fresh worker enrollment,
a real GPU workload and a provider billing cleanup audit.
That run must also establish NVSwitch fabric readiness on the selected A100
and H100 hosts. Hyperstack's published guidance does not specify whether these
US-1 guests need Fabric Manager; the baker requires its CUDA probe to pass.

Official contracts: [custom images](https://docs.hyperstack.cloud/docs/virtual-machines/custom-images/),
[snapshot API](https://docs.hyperstack.cloud/docs/api-reference/snapshots/),
[NVIDIA Ubuntu installation](https://docs.nvidia.com/datacenter/tesla/driver-installation-guide/ubuntu.html),
[Container Toolkit installation](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html),
and [gVisor GPU support](https://gvisor.dev/docs/user_guide/gpu/).
