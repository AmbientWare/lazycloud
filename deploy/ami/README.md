# Connected AWS Node Image Bake

`bake.py` builds the immutable per-region node AMI a connected-AWS managed pool
boots from. For each requested region it:

1. resolves the latest Amazon Linux 2023 x86_64 AMI from the public SSM
   parameter `/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64`;
2. launches one temporary `t3.small` (account default VPC unless `--subnet-id`
   is given) whose user data installs Docker, the pinned Tailscale build from
   `shared.tailscale_install`, the sha-verified release agent executable at its
   production path, pre-pulls the container-worker image by digest, writes
   `/etc/lazycloud-node-image.json`, and powers the instance off;
3. waits for the instance to stop, registers the image as
   `lazycloud-node-<release-version>-amd64` (image, snapshot, instance, and
   volume tagged `cloud-pool:managed-by=control-plane` and
   `lazycloud:release=<version>`), waits until it is `available`, and
   terminates the instance — the temporary instance is terminated best-effort
   even when the bake fails.

The managed-pool bootstrap script treats every install as a guarded no-op when
the dependency is already present and pinned, so nodes booted from a baked
image skip installs and go straight to enrollment. Nodes still boot correctly
from a plain AL2023 AMI; baking only removes the install time.

Because the bake instance downloads the agent anonymously, `bake.py` first
publishes the sha-addressed agent object to its canonical immutable release key
in the public release bucket. `release.py publish` later verifies byte identity
against that same object, so ordering stays safe.

Run it from the repository root (the AWS identity must be able to run
SSM `GetParameter`, EC2 run/describe/terminate instances, create/describe
images, and S3 head/put on the release bucket):

```sh
uv run --group workspace python deploy/ami/bake.py \
  --release-version "$VERSION" \
  --agent-version-dir "dist/agent-binarys/$VERSION" \
  --worker-image "public.ecr.aws/ALIAS/lazycloud/container-worker@sha256:DIGEST" \
  --bucket "$AWS_RELEASE_ASSET_BUCKET" \
  --bucket-region us-east-1 \
  --regions us-east-1
```

The command prints a JSON object mapping region to AMI ID on stdout (progress
goes to stderr), which feeds `release.py stage --cpu-ami-ids`. Reruns are
idempotent: an existing `available` image with the release name is reused and a
`pending` one is awaited.

Cost: one `t3.small` plus a 16 GiB gp3 volume for roughly 5–10 minutes per
region per bake, plus AMI/snapshot storage for the baked image. A `--variant gpu`
bake costs more: a `g4dn.xlarge` and a 40 GiB volume, and the driver install adds
several minutes.

`--variant gpu` bakes the GPU node image. It is the same image plus the pinned
NVIDIA driver and container toolkit, registered as
`lazycloud-node-<version>-gpu-amd64` — a distinct name because an existing image
is matched by name alone, and a GPU bake sharing the CPU name would find that
image and publish a driverless AMI as the GPU catalog entry.

One image serves every card the catalog offers. The driver branch is unified from
Turing through Blackwell, so T4, A10G, L4, L40S, A100, H100, H200 and B200 all
boot the same AMI, and the catalog is one entry per region rather than one per
model.

The driver version is pinned rather than tracking latest, because gVisor's nvproxy
validates the driver ABI it was built against. The gVisor release, this driver and
the node image are one decision; changing any of them alone produces sandboxes
that refuse to start GPU containers.

The bake proves itself before registering: it runs `nvidia-smi -L` and checks
docker reports an `nvidia` runtime. An image that cannot see its own card fails
the bake rather than shipping, launching, enrolling, reporting no GPUs, and
sitting unschedulable while it bills.
