# Connected AWS release assets

Connected AWS capacity has four immutable release inputs:

- a standalone Linux `amd64` agent served by the control plane;
- the exact bundled account-authorization CloudFormation template at an HTTPS
  release URL whose path contains its SHA-256 digest;
- an anonymously pullable container-worker image addressed by manifest digest.
- exact CPU and GPU AMI IDs resolved from the independently published host-image
  catalog.

The customer does not configure any of these. They authorize their AWS account
from the dashboard; platform release and deployment automation owns the assets.

## Release account setup

Deploy `cloudformation.yaml` in `us-east-1`. Amazon ECR Public is managed from
that region. The stack creates the public ECR repository. Terraform in
`deploy/platform-deployment` owns the private S3 release bucket and CloudFront
distribution serving `https://releases.lazycloud.dev`. Pass its `release_bucket`
output as `ReleaseBucketName` to the release stack. For local publication with an authenticated AWS profile, leave the
three GitHub publisher parameters empty. For GitHub publication, pass the
organization, repository, and an existing GitHub Actions OIDC provider ARN
together; the stack then also creates the release-environment OIDC role.

Local one-time setup:

```sh
AWS_PROFILE=default aws cloudformation deploy \
  --region us-east-1 \
  --stack-name lazycloud-release-assets \
  --template-file deploy/aws-release-assets/cloudformation.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides ReleaseBucketName="$OBJECT_STORE_RELEASE_BUCKET"
```

Configure these repository variables from the AWS stack outputs and the
platform's object-store configuration:

```text
AWS_RELEASE_ROLE_ARN
LAZYCLOUD_OBJECT_STORE_ENDPOINT_URL
LAZYCLOUD_OBJECT_STORE_REGION_NAME
LAZYCLOUD_OBJECT_STORE_FORCE_PATH_STYLE
RELEASE_PUBLIC_BASE_URL=https://releases.lazycloud.dev
OBJECT_STORE_RELEASE_BUCKET
AWS_RELEASE_REGION=us-east-1
AWS_RELEASE_CONTAINER_WORKER_REPOSITORY_NAME=lazycloud/container-worker
```

No AWS access keys are stored in GitHub. The OIDC trust accepts only jobs using
the repository's `release` environment.

Trusted publishers configure the regional S3 endpoint and signing settings.
GitHub uses the release role's OIDC credentials for S3, ECR and AMI operations;
operators use `AWS_PROFILE=default`. Do not add object-store access-key secrets
to GitHub. The release role writes and reads only the named release bucket and
cannot delete its objects or administer it.

Customer authorization uses `lazycloud cloud authorize --profile CUSTOMER_PROFILE`.
The CLI checks the caller account and submits the API's exact template body and
named IAM parameters through CloudFormation `CreateStack`. It does not pass an
release URL to CloudFormation. The dashboard keeps the setup instructions visible;
validate the connection after the stack completes.

Private deployment descriptors are separate from public releases. Publish the
Terraform `infrastructure_configuration` output, then configure Actions with its
`s3://` URI:

```sh
terraform -chdir=deploy/platform-deployment output -json infrastructure_configuration > /tmp/infrastructure.json
uv run --group workspace python -m deploy.object_storage publish \
  --uri "$INFRASTRUCTURE_CONFIG_URI" \
  --file /tmp/infrastructure.json
```

The publication command validates the descriptor schema and verifies the bytes
read back from S3. Deploy CI downloads that same private object before rendering
Helm values.

## Release workflow

Run the `Ship` workflow from `main`. Its reusable release workflow then:

1. builds the standalone agent on `linux/amd64` and verifies it on Amazon Linux;
2. builds and pushes `container-worker` to Amazon ECR Public;
3. resolves the current recipe-compatible CPU and GPU AMI catalog and fails if
   the Node Images workflow has not published one for this host recipe;
4. stages the bundled CloudFormation bytes, agent, image digest, and exact AMI
   IDs into one manifest with `deploy/aws-release-assets/release.py`;
5. publishes objects with conditional writes and immutable cache headers, then checks their downloaded SHA-256 digests;
6. downloads every public release object and inspects the worker image with empty credential
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

## What a deployment configures

The deployment records three immutable manifest URLs. `LAZYCLOUD_RELEASE_MANIFEST_URL`
names the control-plane release and authorization template;
`LAZYCLOUD_RELEASE_WORKER_MANIFEST_URL` names the worker image;
`LAZYCLOUD_RELEASE_HOST_MANIFEST_URL` names the host agent executable and AMIs.
Processes must agree on all three. Routine Ship updates control and worker pins
without changing the host pin. `deploy/release.py` writes all three into `.env`
and requires an explicit `--host-manifest-url`; see `deploy/RUNBOOK.md`.

The manifest's `deployment_environment` object is a self-check the release
carries, not settings to transcribe: the schema validates it against the
release's own facts and rejects a manifest whose block disagrees. Published
manifests are immutable, so it stays in the document.

The local agent-binary mount (`LAZYCLOUD_COMPOSE_AGENT_BINARY_DIR`) and connected-AWS
control principal (`LAZYCLOUD_AWS_CONNECTION_CONTROL_PRINCIPAL_ARN`) stay in deployment
configuration. Managed capacity requires all three release pins. Supplier prices
are defined by region in `provider_aws.supplier_prices`.

`capacity_cpu_ami_ids` and `capacity_gpu_ami_ids` come from the current host-image
catalog in Actions. Local publication may still supply them through `--cpu-ami`
and `--gpu-ami`. See `deploy/ami/README.md` for the catalog workflow.

## Focused acceptance

With release AWS identity and Docker registry login already configured:

```sh
uv run --no-project python deploy/agent-binary/build.py build \
  --version "$VERSION" --output dist/agent-binarys --arch amd64

uv run --group workspace python -m deploy.aws-release-assets.release stage \
  --version "$VERSION" \
  --agent-version-dir "dist/agent-binarys/$VERSION" \
  --worker-image "public.ecr.aws/ALIAS/lazycloud/container-worker@sha256:DIGEST" \
  --bucket "$OBJECT_STORE_RELEASE_BUCKET" \
  --public-base-url https://releases.lazycloud.dev \
  --output dist/connected-aws

uv run --group workspace python -m deploy.aws-release-assets.release validate-local \
  --manifest "dist/connected-aws/$VERSION/manifest.json"

uv run --group workspace python -m deploy.aws-release-assets.release publish \
  --manifest "dist/connected-aws/$VERSION/manifest.json"

uv run --group workspace python -m deploy.aws-release-assets.release verify \
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
