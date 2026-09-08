# Hyperstack pooled GPU capacity

The adapter uses the current Infrahub `/v1` API at
`https://infrahub-api.nexgencloud.com/v1`. Authentication uses the `api_key`
header. Request and response models were checked against the official
[Python SDK v1.55.1-alpha](https://github.com/NexGenCloud/hyperstack-sdk-python/tree/67c41d1)
and [API reference](https://docs.hyperstack.cloud/docs/api-reference/).

Initial capacity is US-1, Texas, using `n3-A100-SXM4x8` and
`n3-H100-SXM5x8`. Both contain eight GPUs. Single-GPU A100 and H100 flavors
are documented in CANADA-1, outside this deployment policy.
[Region and hardware availability](https://docs.hyperstack.cloud/docs/hardware/flavors/)
still depends on account access and current stock.

`GET /pricebook` supplies account rates. GPU, CPU, RAM, local disk and one
public IP enter the quote once each. CPU, RAM and local disk currently cost
zero on GPU flavors. Usage bills by the minute; unused machines must be
deleted. No contract or spot request is sent.
[Billing terms](https://docs.hyperstack.cloud/docs/billing/billing-policies/)
require a prepaid balance covering at least one hour of total account usage
before another VM can launch.

## Deployment inputs

The provider binding needs an API key and a US-1 deployment entry with
`environment_name`, `keypair_name`, `image_name` and `recipe_sha256`. The
environment must already exist. The [image baker](../../../deploy/hyperstack/README.md)
publishes the private image and required public keypair. The account API key stays in the control plane.
Workers receive a short-lived, single-use launch token and generate their own
continuing node credential.

The configured image must be a private image in US-1. The release digest
identifies the verified image recipe; setting a digest does not verify an
arbitrary image. Publish the host image before enabling the provider binding.

## Preparing and publishing a host image

`python -m deploy.hyperstack.bake` creates a disposable A100 VM, installs the
host runtime and pinned NVIDIA driver, runs a GPU container through gVisor,
then publishes its cleaned root disk. It uses the native snapshot and image
APIs. See the [build commands](../../../deploy/hyperstack/README.md).

The snapshot must reach `success` before conversion to a private image.
Published images retain their backing snapshot and public keypair, which the
VM API requires at launch. Builder VMs and private SSH keys are removed.
Deleting the backing snapshot also deletes its linked image.

The official [custom-image workflow](https://docs.hyperstack.cloud/docs/virtual-machines/custom-images/)
and [snapshot API](https://docs.hyperstack.cloud/docs/api-reference/snapshots/)
define these operations. Temporary snapshots use
`DELETE /core/snapshots/{snapshot_id}`; private images use
`DELETE /core/images/{image_id}`. Scope cleanup to recorded build resources.

## Lifecycle evidence

Owner tests cover whole-node pricing and a lost create response followed by
late VM discovery and deletion. An unresolved create keeps its durable launch
record and blocks another purchase. It cannot be declared deleted merely
because a list response is empty.
Binding also records the immutable numeric VM ID. Observation, deletion and
storage cleanup use that ID even if an operator renames the VM. The original
launch name remains the node's enrollment identity; ownership-label mismatches
stop cleanup instead of declaring the resource absent.

Read-only account access, pricebook, stock, images, keypairs and snapshot
listing succeeded. The account has no credit and cannot create VMs.
Live acceptance still requires funding, the published host image,
worker enrollment, GPU execution, backfill/preemption, and a provider billing
audit after cleanup. Passing owner tests does not establish those outcomes.
