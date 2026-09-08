# OVHcloud node images

The baker creates one hourly Public Cloud instance, installs the production CPU
runtime, and publishes a private image. It deletes the builder and retains the
published image. Run from the repository root with `uv` and an installed OpenSSH
client. The host image preparation requires Ubuntu 24.04 on x86_64.

Set up the account and API permissions in the
[provider instructions](../../packages/providers/ovh/README.md). The credential
file contains `LAZYCLOUD_PLATFORM_CAPACITY_OVH_CREDENTIALS`. Select its binding
reference and the intended Public Cloud project explicitly.
The `--project-id` argument can be omitted when that file contains `OVH_PROJECT_ID`.

```bash
uv run --python 3.12 python deploy/ovh/bake.py \
  --env-file ~/.lazycloud/operator/deploy.env \
  --credential-ref ovh:us \
  --project-id PROJECT_ID \
  --region US-EAST-VA-1 \
  --base-image-id UBUNTU_24_04_IMAGE_UUID \
  --manifest ~/.lazycloud/operator/ovh-virginia-bake.json
```

Find the immutable Ubuntu image UUID using the project's image list in the OVHcloud
API. The baker validates its distribution, status, and region before creating
anything. Use a new manifest path for each bake. The manifest contains resource
identifiers and progress, never API credentials or SSH private keys.

The baker prints provider operation status, instance state, cloud-init state,
installer output, and snapshot state as it runs. Temporary SSH host keys are pinned
locally. Image preparation clears SSH credentials, cloud-init state, and machine
identity before the snapshot. The API credentials stay on the operator machine.

After publication and cleanup, the command prints the region's `image_id` and
`recipe_sha256` binding. Add them to `LAZYCLOUD_PLATFORM_CAPACITY_OVH`. Build each
region separately. Shared image storage remains billable until the image is retired.

If a request times out, the manifest keeps the unresolved operation. It never
retries an ambiguous create. Resume cleanup with the same manifest:

```bash
uv run --python 3.12 python deploy/ovh/bake.py \
  --env-file ~/.lazycloud/operator/deploy.env \
  --credential-ref ovh:us \
  --manifest ~/.lazycloud/operator/ovh-virginia-bake.json \
  --cleanup
```

Cleanup only touches the instance and unfinished snapshot named by that bake.
It preserves a published image. An unresolved create or snapshot keeps cleanup
incomplete; inspect the named operation in OVHcloud before retiring the manifest.
An unexpected persistent volume also stops cleanup so its owner can be resolved.

Before enabling a binding, run a disposable CPU workload through its published
image and verify enrollment, metering, and complete instance removal. Building an
image alone does not prove that workload path.
