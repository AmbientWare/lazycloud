# Connected AWS Release Assets

Connected AWS capacity has four immutable release inputs:

- a standalone Linux `amd64` agent served by the control plane;
- the exact bundled account-authorization CloudFormation template at an HTTPS
  S3 URL whose path contains its SHA-256 digest;
- an anonymously pullable container-worker image addressed by manifest digest.
- exact CPU and GPU AMI IDs resolved from the independently published host-image
  catalog.

The customer does not configure any of these. They authorize their AWS account
from the dashboard; platform release and deployment automation owns the assets.

## One-Time Release Account Setup

Deploy `cloudformation.yaml` in `us-east-1`. Amazon ECR Public is managed from
that region. The stack always creates a versioned release bucket and public ECR
repository. For local publication with an authenticated AWS profile, leave the
three GitHub publisher parameters empty. For GitHub publication, pass the
organization, repository, and an existing GitHub Actions OIDC provider ARN
together; the stack then also creates the release-environment OIDC role. The
account-level S3 Block Public Access setting must permit the stack's narrowly
scoped public-read bucket policy.

Local one-time setup:

```sh
AWS_PROFILE=default aws cloudformation deploy \
  --region us-east-1 \
  --stack-name lazycloud-release-assets \
  --template-file deploy/aws-release-assets/cloudformation.yaml \
  --capabilities CAPABILITY_NAMED_IAM
```

When GitHub publication is configured, set these repository variables from the
stack outputs:

```text
AWS_RELEASE_ROLE_ARN
AWS_RELEASE_ASSET_BUCKET
AWS_RELEASE_REGION=us-east-1
AWS_RELEASE_CONTAINER_WORKER_REPOSITORY_NAME=lazycloud/container-worker
```

No AWS access keys are stored in GitHub. The OIDC trust accepts only jobs using
the repository's `release` environment.

## Release Workflow

Run the `Ship` workflow from `main`. Its reusable release workflow then:

1. builds the standalone agent on `linux/amd64` and verifies it on Amazon Linux;
2. builds and pushes `container-worker` to Amazon ECR Public;
3. resolves the current recipe-compatible CPU and GPU AMI catalog and fails if
   the Node Images workflow has not published one for this host recipe;
4. stages the bundled CloudFormation bytes, agent, image digest, and exact AMI
   IDs into one manifest with `deploy/aws-release-assets/release.py`;
5. publishes objects with SHA-256 checksums and immutable cache headers;
6. downloads every S3 object and inspects the worker image with empty credential
   directories, proving customer nodes can access them anonymously. Baked AMIs
   are private platform images, so the anonymous verify only format-checks them
   and notes the skip; run `release.py verify --aws-cli-verify` with platform
   credentials to describe each AMI.

The local staged bundle retains the same verified agent used for publication in
the directory shape consumed by the Compose control plane:

```text
dist/connected-aws/$VERSION/
  manifest.json
  objects/
    connection-template.json
  agent-binarys/
    $VERSION/
      lazycloud-agent-linux-amd64
```

The agent object's `local_path` is relative to the bundle. No machine-specific
artifact root is written to the published manifest. `validate-local` checks the
retained executable and derives its absolute `agent-binarys` root from the local
manifest path.

## What A Deployment Configures

`LAZYCLOUD_RELEASE_MANIFEST_URL` — the published `manifest_public_url`, and
the only value a deployment takes from a release. The control plane fetches that
manifest at startup and resolves the agent artifact version and digest, the URL
that serves it, the container-worker image, the customer authorization
template, and both host AMI catalogs from the release itself. None of the six
are environment-readable, so no deployment can hold five of them from one
release and one from another. `deploy/release.py` writes the URL into `.env`;
see `deploy/RUNBOOK.md`.

The manifest's `deployment_environment` object is a self-check the release
carries, not settings to transcribe: the schema validates it against the
release's own facts and rejects a manifest whose block disagrees. Published
manifests are immutable, so it stays in the document.

What no release can know stays authored beside the deployment: the local
agent-binary mount (`LAZYCLOUD_COMPOSE_AGENT_BINARY_DIR`), instance price estimates
(`LAZYCLOUD_AWS_CAPACITY_INSTANCE_HOURLY_MICROS`), and the connected-AWS
control principal (`LAZYCLOUD_AWS_CONNECTION_CONTROL_PRINCIPAL_ARN`). Managed
capacity is those plus the three the release publishes or none of them, and a
deployment missing either half fails at startup naming which half it is.

`capacity_cpu_ami_ids` and `capacity_gpu_ami_ids` come from the current host-image
catalog in Actions. Local publication may still supply them through `--cpu-ami`
and `--gpu-ami`. See `deploy/ami/README.md` for the catalog workflow.

## Focused Acceptance

With release AWS identity and Docker registry login already configured:

```sh
uv run --no-project python deploy/agent-binary/build.py build \
  --version "$VERSION" --output dist/agent-binarys --arch amd64

uv run --group workspace python deploy/aws-release-assets/release.py stage \
  --version "$VERSION" \
  --agent-version-dir "dist/agent-binarys/$VERSION" \
  --worker-image "public.ecr.aws/ALIAS/lazycloud/container-worker@sha256:DIGEST" \
  --bucket "$AWS_RELEASE_ASSET_BUCKET" \
  --region us-east-1 \
  --output dist/connected-aws

uv run --group workspace python deploy/aws-release-assets/release.py validate-local \
  --manifest "dist/connected-aws/$VERSION/manifest.json"

uv run --group workspace python deploy/aws-release-assets/release.py publish \
  --manifest "dist/connected-aws/$VERSION/manifest.json"

uv run --group workspace python deploy/aws-release-assets/release.py verify \
  --manifest "dist/connected-aws/$VERSION/manifest.json"
```

Staging, local validation, and publication are idempotent for identical bytes
and fail on any collision. Local validation verifies the canonical manifest,
every retained artifact digest and size, the executable mode, and the bundled
connection-template policy without contacting AWS. Release objects and image
digests are durable production artifacts; retain the staged bundle at the path
used for Compose activation so its read-only agent-binary mount remains valid.
Published metadata stays portable.
Temporary live-acceptance stacks, connections, pools, and instances are cleaned
separately after the provider lifecycle smoke.
