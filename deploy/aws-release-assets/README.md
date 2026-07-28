# Connected AWS Release Assets

Connected AWS capacity has three immutable release inputs:

- a standalone Linux `amd64` agent served by the control plane;
- the exact bundled account-authorization CloudFormation template at an HTTPS
  S3 URL whose path contains its SHA-256 digest;
- an anonymously pullable container-worker image addressed by manifest digest.

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

Tag or dispatch `.github/workflows/release.yml`. The connected-AWS job:

1. builds the standalone agent on `linux/amd64` and verifies it on Amazon Linux;
2. builds and pushes `container-worker` to Amazon ECR Public;
3. bakes the per-region node AMI with `deploy/ami/bake.py` (skippable through
   the `bake_ami` dispatch input for a template-only release);
4. stages the bundled CloudFormation bytes, agent, image digest, baked CPU AMI
   catalog, and deployment settings with `deploy/aws-release-assets/release.py`;
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
artifact root is written to the published manifest. The Compose configuration
command validates the retained executable and derives its absolute
`agent-binarys` root from the local manifest path before activation.

The uploaded workflow evidence contains `manifest.json`. Its
`deployment_environment` object is the canonical Compose configuration.

```text
agentArtifact.version                     LAZYCLOUD_AGENT_BINARY_VERSION
agentArtifact.url                         agent object public_url
agentArtifact.sha256ByArch.amd64           manifest agent_artifact_sha256
awsCapacity.connectionTemplateUrl          LAZYCLOUD_AWS_CONNECTION_TEMPLATE_URL
awsCapacity.workerImageDigest              LAZYCLOUD_AWS_CAPACITY_WORKER_IMAGE_DIGEST
awsCapacity.agentArtifactUrl               LAZYCLOUD_AWS_CAPACITY_AGENT_BINARY_URL
awsCapacity.cpuAmiIds                      LAZYCLOUD_AWS_CAPACITY_CPU_AMI_IDS
```

`LAZYCLOUD_AWS_CAPACITY_CPU_AMI_IDS` is present when the release baked node
AMIs; it is the JSON region-to-AMI map from `manifest.capacity_cpu_ami_ids`
(see `deploy/ami/README.md`).

When `agentArtifact.url` is configured, the chart downloads and verifies the
binary in an init container before the API starts. `agentArtifact.volume.existingClaim`
remains available for operators that mirror immutable artifacts into cluster
storage; the URL and claim modes are mutually exclusive.

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
