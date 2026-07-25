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
  --agent-version-dir "dist/agent-artifacts/$VERSION" \
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
region per bake, plus AMI/snapshot storage for the baked image.

GPU AMIs are out of scope for now: only the CPU catalog
(`LAZYCLOUD_AWS_CAPACITY_CPU_AMI_IDS`) is baked and published;
`LAZYCLOUD_AWS_CAPACITY_GPU_AMI_IDS` stays operator-provided until a GPU bake
variant exists.
