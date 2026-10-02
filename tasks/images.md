# Images

Outcome: `Image(python_packages=["numpy"])` on a function deploys with an
`Image` step that reads `python 3.12 · built` the first time and
`python 3.12 · cached` after that, in any workspace, and the function imports
numpy. Editing the function's code never rebuilds. Parity section "Images" in
tasks/parity.md.

## Design

- Identity. The server renders a definition into a Dockerfile with every
  `FROM` pinned by digest. A registry HEAD through go-containerregistry turns
  a tag into a digest; anonymous lookups are cached for five minutes, lookups
  with credentials never are. The image digest is the SHA-256 of the rendered
  Dockerfile, the architecture and the build context digest. `img_` plus 24
  hex characters of it is the public image id. Equal definitions in any
  workspace share one image.
- Schema (`migrations/0005_images.sql`). `images` is global: digest, id,
  Dockerfile, Python version, architecture and, once published, the pullable
  reference. `workspace_images` records which workspaces resolved an image;
  only they can read or deploy it, because resolving proves the workspace
  holds every input (context, base access). `image_builds` has at most one
  `building` row per image (partial unique index), so a concurrent request
  joins it. `image_build_logs` holds build output.
- A definition whose Dockerfile is a single `FROM` of a public image needs no
  build; its reference is the base by digest. `Image()` is
  `python:3.12-slim` pinned this way.
- Builds run in execution containers. `containers.image_build_id` marks a
  build container; `release_id` became nullable and a check keeps exactly one
  owner. The build request inserts the build and asks execution for a
  pending container in one transaction under the image row lock. Scheduling
  places it like any container; the host session derives
  `StartContainer.build` from it and resends it after reconnects.
- The agent runs `moby/buildkit:v0.33.1-rootless` (`buildctl-daemonless.sh`,
  pinned by digest) with the process sandbox, registry cache import and
  export, and pushes by digest to the platform registry. It streams output
  with `AppendImageBuildLogs` and reports the digest or the failure with the
  last 20 lines through `CompleteImageBuild`, then reports the exit. The
  server HEADs the digest in the registry before it publishes the image.
- Recovery (scheduler, woken by `lc_image_build`, every 10 s otherwise). A
  build container that stops without an outcome (host lost, start failure,
  crash, agent restart) gets one more container; the second loss fails the
  build. A failed build step fails it at once. A build past one hour fails
  and its container is stopped through execution.
- Hosts pull by digest through the Docker Engine. The platform registry login
  travels in `StartContainer.image_auth` and goes only to the pull call.
- Base image credentials become a registry login on the server (GHCR, ECR
  through `GetAuthorizationToken`, GCR and pkg.dev, ACR, NGC, Docker Hub,
  basic pairs). They apply to the base image's registry only and sit in
  `image_builds.registry_auth` until the build ends.
- Context archives (`add_local_path`, `from_dockerfile`, project factories)
  are deterministic zips stored through the source upload API. Project
  contexts hold manifests and local dependencies only.
- Lock order: image row, then build row. Execution's container locks come
  first only inside execution transitions, which never touch builds.

## Delivered

| Parity item | Evidence |
| --- | --- |
| `Image(...)` with Python 3.10-3.14 or a patch release, packages, commands, base image and creds, env vars, image id, architecture | `test_sdk_image_build.py` (definition mapping, version forms, architecture); e2e numpy deploy |
| `from_registry`, `from_dockerfile`, `from_id` | e2e private `from_registry`; `test_sdk_image_build.py` (`from_id`, dockerfile context) |
| `from_uv`, `from_poetry`, `from_pyproject`, `from_micromamba` | e2e `from_uv` deploy and call; SDK tests for identity stable across code edits |
| Builders `add_commands` ... `with_docker` | SDK mapping tests; `with_secrets` and `build_with_gpu` reject as `unsupported` (gaps) |
| `verify`, `exists`, `build`, `spec`, `get_credentials_from_env` | `test_sdk_image_build.py` |
| Credential names per registry | `TestRegistryCredentialNames`; e2e private registry with basic auth, and the wrong-password error |
| Content-addressed cache, code edits never rebuild | `TestEqualDefinitionsShareOneImageAuthorizedPerWorkspace`, `TestConcurrentBuildRequestsJoinOneBuild`; e2e `from_uv` code edit shows `cached` |
| Terminal `Image` step (preparing, cached or built, indented logs, last 3 live, last 20 on failure) | `test_sdk_image_build.py` output tests; e2e deploy output |

Go tests at owner boundaries, all against real PostgreSQL, a real registry,
Docker and BuildKit: `TestCompletedBuildPublishesOnlyAPushedDigest`,
`TestLostBuildContainerRetriesOnceThenFails`,
`TestBuildPastItsDeadlineFailsAndStopsItsContainer`,
`TestDeployableNeedsAReadyImageForTheRuntime`,
`TestDefinitionsNeedReadableInputs`,
`TestAgentBuildsPushesAndPullsAnImageByDigest`,
`TestAgentReportsAFailedBuildWithItsOutputTail`,
`TestAgentStopsABuildWithoutAnOutcome`.

The parity sweep (2026-10-01, compose project `parity-sweep`) built
`Image.from_poetry` (poetry.lock), `Image.from_pyproject` and
`Image.from_micromamba` (conda-forge `six`) on a private stack; each function
imported `six` 1.17.0, the micromamba one from `/opt/micromamba/bin/python3`,
and one deploy prepared the three images at once.

The integrated run used a private stack (compose project `imgpkt`, ports
36xxx): numpy and `from_uv` images built, deployed and called; a private base
with `REGISTRY_USERNAME`/`REGISTRY_PASSWORD` built; an agent restart during a
build made the server start a second build container that finished.

## Measurements

One host (24 CPUs, Docker 29, runc), local registry 3.1.2, Docker Hub for
the base image.

| What | Result |
| --- | --- |
| Build request for a ready image (numpy), 50 calls | p50 2.1 ms, p95 2.8 ms; 68 ms on the call that HEADs the base |
| Resolve `Image()`, 50 calls | p50 2.1 ms, p95 2.4 ms |
| Cold build of `python_packages=["numpy"]`, request to published | 11.8 s: placement and builder start 0.1 s, BuildKit start 0.4 s, base metadata 1.1 s, base pull 5.0 s, pip 3.8 s, export and push 1.7 s |
| First deploy with that build, CLI | 14.2 s |
| Host pull of the numpy image by digest, base layers present | 0.36-0.40 s (196 MB image, numpy layer only transferred) |
| Cold `.remote()` of the numpy function after deploy | 1.4 s, pull included |

The reference was not measured under the same conditions.

## Intentional differences

- Default images start from `python:<version>-slim` (Debian slim, CPython)
  instead of Debian slim plus uv-managed CPython. `Image()` then needs no
  build at all, and a patch version picks the matching official tag.
- A Dockerfile has no stdlib bytecode precompile step; pip and uv installs
  compile bytecode for installed packages.
- Credentials apply to the base image's registry only. The reference sent
  generic pairs to every registry the build touched.
- `GITHUB_TOKEN` alone logs in to GHCR with a placeholder user name; the
  reference sent it as an identity token, which GHCR does not accept.
- Every shell step is a Dockerfile heredoc, so the parser never reads the
  command and a multi-line command stays one step.
- `force_rebuild=True` rebuilds a published image for the requesting
  workspace only; other workspaces keep the published image.
- A release pins the image reference by digest when it is created
  (`ImageSpec.reference`, read-only). A rebuild reaches a function only
  when it is deployed again.
- User Dockerfiles may not use parser directives (`# syntax=` would run a
  frontend image), variables in image names, or `COPY --from` and
  `RUN --mount from=` naming an image. Naming the image in
  `FROM ... AS stage` does the same and is access-checked.
- Base images must come from public registries, and images in the platform
  registry are used by id through `Image.from_id`.
- `ImageSpec.base` is empty for the platform image instead of the Debian
  digest.
- `Image.verify()` returns an `ImageVerification`; `ImageBuildResult`
  has no `responses` and `Image` no `context_object_id`. The client argument
  is optional and `workspace=` selects the workspace.

## Review fixes

| Finding | Fix | Regression test |
| --- | --- | --- |
| Images named outside FROM, directives, injected instructions | BuildKit's parser reads the final Dockerfile; every FROM is pinned and access-checked, other image references are refused, values with line breaks are refused, shell steps are heredocs with a command-derived delimiter | `TestDockerfilesCannotNameUncheckedImages`, `TestDefinitionValuesCannotInjectInstructions` |
| Platform registry images as a base | User references to the platform registry are refused | `TestBaseImagesMustBePublicRegistries` |
| Shared cache and global force | Caches are scoped per workspace; forced rebuilds publish to `workspace_images.reference`; releases pin `ImageSpec.reference` | `TestForcedRebuildsAndCachesStayInTheirWorkspace`, `TestDeployPinsTheImageReference` |
| SSRF through base images and token realms | Private, loopback and link-local registry hosts are refused by name; every dial except the platform registry passes a Control hook that refuses non-public addresses; no proxy | `TestBaseImagesMustBePublicRegistries`, `TestRegistryLookupsNeverDialPrivateAddresses` |
| Unbounded build output | An attempt stores at most 8 MiB and 100,000 lines, counted under the build row lock, with one truncation marker | `TestBuildOutputIsCappedPerAttempt` |

`migrations/0005_images.sql` changed in place (forced builds, workspace
references, log counters). Local databases that applied the earlier 0005
need a reset.

Host registry logins (deploy review, PR #443): on ECR every host command
gets a login of its own, minted from a session of the registry host role
whose policy names only the repositories it needs. A built image has a
repository named by its digest, so equal definitions still share one image
and one build, and a pull login reaches that image's repository alone.
Caches and filesystem snapshots have a repository per workspace. A build
may push only to its image and its workspace's cache, a snapshot only to
its workspace's snapshots; nothing else pushes. Only a build on a platform
host publishes the shared image: a forced rebuild, a build on a connected
account's or joined machine's host, and every build of a workspace bound
to a connected account push to `workspace-images/<workspace>/<digest>`
and publish for their workspace alone
(`TestBuildsOnCustomerHostsNeverPublishForOtherWorkspaces`). The server's own login
reads only. Tests: `TestHostLoginsAreScopedToTheCommandsRepositories`,
`TestRepositoryOfAReference`, `TestCompletedBuildPublishesOnlyAPushedDigest`,
`TestForcedRebuildsAndCachesStayInTheirWorkspace`.

## Gaps

- gVisor build isolation: builders run with runc, seccomp and AppArmor
  unconfined and unmasked system paths. A malicious step can reach its own
  BuildKit daemon and the build's push login. No runsc locally.
- Build network: the agent runs builders on `-build-network` (`host`
  locally so they reach the loopback registry). Production needs an
  isolated network with registry-only egress, away from instance metadata.
- ECR base images: the `GetAuthorizationToken` exchange is unverified
  without AWS credentials; GCR, ACR and NGC logins are covered by the name
  mapping test only.
- `build_with_gpu` and `with_secrets` reject as `unsupported`. Workspace
  secrets exist now, so `with_secrets` needs the images owner to resolve
  them at build time and key the image identity on their versions (about a
  day); GPU builds need GPU build capacity. `machine=` on `build()` is
  unsupported: builds do not use machine pinning.
- Architecture: arm64 is part of the identity and the build platform, but
  hosts do not report an architecture, so placement cannot match it.
- Base registry logins are stored in plaintext on the build row until it
  ends; wrap them with the secrets packet's data keys once those exist.
- Each build starts BuildKit with an empty store, so base layers are pulled
  per build (5 s for python slim). A per-host layer cache is a follow-up.

## Shared changes

- `Propose:` commits: containers may belong to an image build
  (`containers.go`, `reports.go`, three query files), and two NOTIFY channels
  in `database/listen.go`.
- `hostsession.NewServer` takes the images owner; `api.Owners` has `Images`;
  `DeployApp` checks image ids before control deploys.
- OpenAPI: image paths and schemas, `ImageSpec.image_id`, error code
  `unavailable`. The new enums share values with `TaskStatus`, so
  oapi-codegen now prefixes `TaskStatus` constants (`apitypes.TaskStatusQueued`).
- Proto: `StartContainer` fields 20-22, `CompleteImageBuild` and
  `AppendImageBuildLogs`.
- `compose.yaml` adds `registry` (3.1.2, the current Distribution release);
  `deploy/local/run.sh` sets the registry and `-build-network host`.
- The server now requires `LAZYCLOUD_IMAGE_REGISTRY`.
