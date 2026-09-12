# Connected AWS node images

Node images contain host dependencies that are too slow or risky to install on
every scale-up. They do not contain a LazyCloud release, agent binary, or worker
image.

The `Connected AWS Node Images` workflow runs only by explicit dispatch from
`main`. Its CPU and GPU jobs run in parallel. Each job resolves the latest Amazon
Linux 2023 x86_64 image, launches a temporary bake instance, installs Docker,
host networking tools, zram, and SSM, then registers an immutable AMI. The GPU variant
also installs and verifies the pinned NVIDIA driver and container toolkit.

The instance syncs its filesystem and prints the completion marker, then stays
running. The controller reads that marker from EC2 console output, stops the
instance, and verifies it is stopped before capturing the image. A stopped
instance without a verified marker fails the bake.

Both images use the same host recipe digest. The digest covers the bake program,
the generated runtime-only agent installer, and `gvisor-version`. AMI names and
tags carry that digest, so rerunning an unchanged recipe reuses the same images.

After both bakes pass, `catalog.py publish` verifies that every AMI is available
and has the expected managed and recipe tags. It writes an immutable catalog and
then replaces the current pointer served by the release distribution:

```text
connected-aws/node-images/catalogs/<catalog-sha256>.json
connected-aws/node-images/current.json
```

Publication and AMI verification use the release AWS identity, or the operator's
`default` profile. The S3 endpoint and region come from the deployment configuration.
To publish a fresh catalog for existing verified AMIs:

```sh
uv run --group workspace python -m deploy.ami.catalog publish \
  --cpu-ami-ids "$CPU_AMI_IDS" --gpu-ami-ids "$GPU_AMI_IDS" \
  --source-revision "$(git rev-parse HEAD)" \
  --bucket lazycloud-production-releases \
  --public-base-url https://releases.lazycloud.dev
```

The release workflow reads `current.json` before building application artifacts.
It fails if the catalog is missing or its recipe digest differs from the checked
out revision. Ship never starts an EC2 image bake.

Run a bake locally from the repository root with an AWS identity that can use the
release bake role:

```sh
uv run --group workspace python -m deploy.ami.bake \
  --regions us-east-1

uv run --group workspace python -m deploy.ami.bake \
  --variant gpu \
  --regions us-east-1
```

`bake.py` prints a JSON region-to-AMI map on stdout and progress on stderr. CPU
bakes use `c7i.large` with a 16 GiB volume. GPU bakes use `g4dn.xlarge` with a 40
GiB volume so the bake can prove `nvidia-smi` and the Docker NVIDIA runtime before
publishing an image.

One GPU AMI serves Turing through Blackwell. The NVIDIA driver build is pinned in
`bake.py`, and the worker's gVisor version is pinned in `gvisor-version`. Changing
either changes the host recipe and blocks Ship until the Node Images workflow
publishes a matching catalog.

New nodes download the current agent through the bootstrap installer. The agent
pulls the release's exact worker digest before it writes its runtime-ready marker.
