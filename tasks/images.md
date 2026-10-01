# Images

Outcome: `Image(python_packages=["numpy"])` on a function deploys with an
`Image` step that reads `python 3.12 · built` the first time and
`python 3.12 · cached` after that, in any workspace, and the function imports
numpy. Editing the function's code never rebuilds. Parity section "Images" in
tasks/parity.md.

## Design

- Identity. The server renders the image definition into a Dockerfile with
  every `FROM` pinned by digest (tag to digest through a registry HEAD with
  go-containerregistry, cached for five minutes when no credentials are
  involved). The image digest is the SHA-256 of the rendered Dockerfile, the
  architecture, the build context digest and the GPU hint. `img_` plus 24 hex
  characters of it is the public image id. Equal definitions in any workspace
  share one image.
- Schema (`migrations/0005_images.sql`). `images` is global and holds the
  digest, id, Dockerfile, Python version, architecture and, once built, the
  pullable reference. `workspace_images` records which workspaces resolved an
  image; only those can deploy it. `image_builds` has one `building` row per
  digest (partial unique index), so a concurrent build request joins it.
  `image_build_logs` holds build output.
- A definition whose Dockerfile is a single `FROM` of a public image needs no
  build: its reference is the base by digest. `Image()` is `python:3.12-slim`
  pinned this way, so the default image costs one HEAD per five minutes.
- Builds run in execution containers. `containers.image_build_id` marks a
  build container (`release_id` becomes nullable; a check keeps exactly one
  owner). Images asks execution for a pending container in the build's
  transaction; scheduling places it like any other; the host session sends
  `StartContainer.build`. The agent runs `moby/buildkit` rootless
  (`buildctl-daemonless.sh`) with registry cache import and export, pushes by
  digest to the platform registry and reports the digest with
  `CompleteImageBuild`. The server checks the digest exists in the registry
  before it records the reference.
- A build container that stops without an outcome (host lost, start failure,
  crash) gets one more container; the second loss fails the build. A build
  error from BuildKit fails it at once. A build past one hour is stopped.
  The scheduler runs this recovery, woken by `lc_image_build`.
- Hosts pull by digest through the Docker Engine. Registry credentials travel
  in `StartContainer.image_auth` and go to the pull call only; nothing is
  written to disk or into the container.
- Base image credentials from the SDK become a Docker auth entry per
  registry (GHCR, ECR through `GetAuthorizationToken`, GCR/pkg.dev, ACR, NGC,
  Docker Hub and basic pairs). They live on the `image_builds` row until the
  build ends.
- Context archives (`add_local_path`, `from_dockerfile`, project factories)
  reuse source uploads: a deterministic zip registered by SHA-256 in the
  workspace. Project contexts hold manifests and local dependencies only, so
  code edits leave the identity unchanged.

## Plan

- [ ] Contracts: migration, OpenAPI image paths, proto fields 20-22 and the
      two build RPCs
- [ ] `internal/images`: rendering, identity, registry resolution,
      credentials, resolve/build/complete/recover/logs
- [ ] Execution additions for build containers; null-release handling
      (Propose commit)
- [ ] Host session and agent: build containers, authenticated pulls
- [ ] API handlers, server and scheduler wiring, compose registry
- [ ] Python SDK image module and deploy `Image` step; remove image options
      from `Function.unsupported_options()`
- [ ] Tests at real PostgreSQL, Docker, BuildKit and registry
- [ ] Measurements

## Intentional differences

(filled in as they land)

## Gaps

(filled in as they land)
