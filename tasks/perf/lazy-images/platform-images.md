# Platform images

## Scope

The images the agent itself runs (the BuildKit builder, the volume mount
image, the pod network holder and any other image the agent pulls on its own)
come through the snapshotter with their own grants, like every workload
image. Today they pull plainly, and containerd reuses a snapshot by chain ID,
so a platform image can end up reading through a tenant's lazy layer and its
grant (snapshotter review, finding 2).

Owns:

- `internal/images`: server-side conversion of platform-global references
  without a build container: copy the image into the platform registry,
  convert each layer with `imagefs.Convert`, record it as a platform-global
  image. Deduped (one conversion per reference across server replicas, by an
  advisory lock or the existing build dedupe), retried on transient errors.
- `contracts/host/v1/host.proto` fields 110-119: the agent names its platform
  image references at session open.
- `internal/hostsession`: convert them on demand and send their grants with
  the session and its refreshes, through the grants path.
- the agent: pull them through `ensureLazy`; delete the plain `docker pull`
  path in `internal/agent/source.go`.

Stay off the snapshotter's internals, the build path and the format.

## Evidence to record

- Owner tests against real Postgres for dedupe and failure.
- An agent harness test: a platform image and a tenant image sharing a base
  layer each read through their own grant; expiring the tenant's grant leaves
  the platform image readable.
- A local VM run (`deploy/local/host-vm.sh`) where a build and a volume mount
  run with no plain pull.

## Progress

On `perf-lazy-platform-images` from `perf-plan` (071eb734); draft PR #495.

- The agent pulls on its own exactly two images, named in
  `internal/platformimages` (no database access; agent and server import
  it): `Builder`, the BuildKit builder, and `Mount`, which runs volume
  mounts, the network holder of checkpointable containers and the network
  policy helper. The agent's `-mount-image` flag is gone. The server
  refuses a Hello naming any other image (`InvalidArgument`) and converts
  only these. `Hello.platform_images = 110`
  names both; `ServerMessage.platform_images = 110` (`PlatformImages` of
  `PlatformImage`: reference as named, image, auth, platform, layers,
  failure) answers. `imageCache.ensure` and its `docker pull` are gone;
  `Agent.platformImage` waits for the server's answer, grants the layers,
  pulls with `ensureLazy` and returns the copy to run. Every
  `PlatformImages` also queues its grants as refreshes, so long-running
  mount and holder containers keep reading.
- Migration 0006: `platform_images` (reference, architecture, mirror,
  lease, last failure). It reads and writes no table it creates.
- `images.ConvertPlatformImage`: takes the row's one-minute lease, renewed
  every 20 s while it converts (one conversion of a reference and
  architecture at a time across replicas; a dead owner's lease lapses
  within a minute; an owner whose lease another took stops; the outcome is
  recorded only under the lease token, and every record before it is
  idempotent; a conversion still ends after 15 minutes), copies the image for the host architecture into
  `<Repository>/platform/<registry>/<path>@<digest>` (the platform
  manifest's digest), converts each layer no shared pair holds with
  `imagefs.Convert` on the server's disk, and stores the pairs through
  publish's own offers, multipart uploads and checks (`recordLayers`,
  `presignUploads`, `checkConversions`, `endUploads`), PUTting to the URLs
  itself. Pairs are shared, like a platform host's. Registry reads and
  store writes retry 3 times; a failure of the image (a layer imagefs
  refuses, a media type, over 4 GiB) is a `ConversionError` retried after
  10 minutes; other failures are transient, retried after 30 s. The
  source must name its digest and be public, or in the platform
  registry outside the workload repositories (a host cannot have a
  workspace's image copied).
- `images.PlatformPulls` reads every named reference in one query and
  returns the copy with a pull login valid for at least 30 minutes (the
  static login, or an ECR login reused while it outlasts that), a failure
  and when it may be retried, or nothing yet.
- `hostsession`: the session answers the Hello's images before
  any start, sends each converted one once with its grants, then again when
  a third of the grant life passed (with a fresh login), and each failure
  once; a failed image is read again once its retry period passed. The
  Hello also names the copies the host's mount and holder containers run
  (`running_platform_images = 111`, at most 16), and the session renews
  grants for those the server recorded, so a container started from an
  image a newer agent no longer names keeps reading. It reads the images
  only while one waits or is due. A waiting image starts a conversion on
  this server (at most 2 at once, owned by the
  server, stopped by `Shutdown`, waited by `Wait`) and subscribes to
  `ChannelImageBuild` key `platform-images`, which every outcome
  announces. Sent copies are recorded as uses, so the layer sweep keeps
  their pairs while hosts run.

## Evidence

`go test -race` with Postgres from `compose.test.yaml`, a Garage of my own
(compose project `perf-lazy-platform-images`, ports 24930 and 24933) and a
registry per test:

- `TestPlatformImagesConvertOnceAcrossReplicas` (images): a held lease
  converts nothing and the image waits; once it lapses, 6 concurrent
  requests over two replicas convert once (2 shared pairs, no upload left
  over, which a second conversion would leave); the copy's pairs hold the
  config's diff_ids; an image on that base converts only its new layer.
  Without the lease condition it fails.
- `TestPlatformImageFailuresAreTypedAndRetried` (images): a layer imagefs
  refuses is a `ConversionError` that requests see and that is not
  converted again until its retry period passed; an unreachable registry
  is transient, keeps requests waiting and is tried again after its delay;
  an unpinned, a private and a workload-repository reference are refused
  with no row.
- `TestSessionsSendPlatformImagesOnceConverted` (hostsession): the first
  answer carries the builder's converted copy with its grant and the mount
  image's recorded failure; once another replica records the mount
  image's copy the session sends it; grants are renewed before they
  expire; sent copies count as uses. `TestHelloNamesOnlyPlatformImages`:
  a Hello naming another image is refused and nothing converts.
- `TestConvertRefusesLayersItCannotIndex` (imagefs): a stream cut inside a
  header and a malformed header are `ErrInvalidLayer`, so conversion and
  publish fail the image instead of retrying. Before, they were plain read
  errors.
- `TestPlatformAndTenantImagesReadThroughTheirOwnGrants` (agent harness,
  CI `host-runtime`): a tenant image on the mount image's base is pulled
  lazily first, so the base's snapshot is the tenant pull's; its grant
  expires and its pairs are deleted; a checkpointable pod then starts, and
  its network holder runs the mount image, reading the base through the
  platform grant. A restarted agent's Hello names the holder's copy.
- `TestPlatformLeasesAreRenewedAndTakenOverFromACrashedOwner` (images,
  2 s lease): a conversion running for 2.5 leases keeps its lease; when
  another owner takes it the first stops; that owner's lease, never
  renewed, is taken over once it lapses. Without renewal it fails.
- `TestAFailedPlatformImageIsReadAgainAfterItsRetryPeriod`,
  `TestRunningPlatformCopiesKeepTheirGrants` (hostsession).
- `TestAFailedStartDoesNotWaitForItsHoldersImage` (a start failing while
  its holder waits for the mount image ends `START_FAILED` at once, since
  the holder's start is cancelled) and
  `TestAPlatformFailureLastsOnlyItsSession` (the agent drops failures at
  session open and waits for the new answer) (agent harness).
- ECR: the control plane role could only read workload repositories, so
  copying into `<prefix>/platform/` would have been refused. Statement
  `CopyPlatformImages` in `deploy/terraform/platform-deployment/iam.tf`
  lets it create and push repositories there; the workload images
  creation template (`CREATE_ON_PUSH`, prefix `<name>/workload-images`)
  already covers them.

Local VM (`deploy/local/host-vm.sh` from a copy of `run.sh` on this
packet's own compose project and ports, Lima home under /tmp; all removed
after): the server converted both platform images from Docker Hub within
21 s of the agent's Hello (busybox 1 layer, BuildKit 6, 124 MB stored). A
function with a built image (`add_python_packages(["six"])`) and a volume
then ran: the build ran in the builder copy, converted 4 new layers and
published; the task wrote and read its volume through the busybox copy's
mount container. On the VM, containerd held only
`127.0.0.1:25100/lazycloud/platform/...` copies and the built image, its
content store only manifests and configs (largest 5.9 kB), and the
registry served containerd only manifests and configs; layer blobs went
only to the server's conversion.

## Intentional differences

- The agent's platform images come from the platform registry's copy, not
  Docker Hub.

## Gaps and unverified boundaries

- `CopyPlatformImages` needs a `terraform apply` of
  `deploy/terraform/platform-deployment` before this ships; Ship applies
  no Terraform. Pushing to ECR is untried.

- A layer stream a network failure truncates is now a content failure
  too: imagefs cannot tell it from a truncated layer.
