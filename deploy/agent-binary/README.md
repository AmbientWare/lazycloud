# Build agent releases

This build produces Linux `amd64` and `arm64` agent releases. Each is a gzip
tarball of a PyInstaller directory build, with the `lazycloud-agent` executable
at its root. It uses uv-managed Python 3.12 on AlmaLinux 8 with glibc 2.28 so
the release runs on Amazon Linux 2023 without a host Python installation.

A directory build starts without unpacking anything. A single-file build
unpacks its whole runtime into `/tmp` on every start, which a resumed machine
pays before its agent can connect.

Build and stage both architectures from the repository root:

```sh
uv run --no-project python deploy/agent-binary/build.py build \
  --version 0.1.0 \
  --output dist/agent-binarys
```

The command writes a local bundle for release publication:

```text
dist/agent-binarys/
  0.1.0/
    manifest.json
    lazycloud-agent-linux-amd64.tar.gz
    lazycloud-agent-linux-arm64.tar.gz
```

The Docker build runs `--help` on each target's executable in both its build
environment and Amazon Linux 2023 before export. The stager checks each archive
holds the executable, then records its SHA-256 digest in `manifest.json`.

## On a machine

The install script unpacks a release into `lib/lazycloud-agent/<digest>/`, named
by the archive's verified digest, and links `bin/lazycloud-agent` to the
executable inside it. The prefix is `/usr/local` for root and `~/.lazycloud`
otherwise. The agent reports that directory name as its digest. To update, it
unpacks the next release beside the current one and moves the link. Once the
new release has streamed, only it and the previous one are kept.

Mount the output root read-only: `LAZYCLOUD_COMPOSE_AGENT_BINARY_DIR` is the
host directory bound in, `LAZYCLOUD_AGENT_BINARY_DIR` where the control plane
reads it inside the container. The mount is
the deployment's; which version it holds and what that version must hash to are
facts of the release that filled it, so neither is set by hand. The control
plane resolves both from the release manifest at
`LAZYCLOUD_RELEASE_MANIFEST_URL` (see `deploy/aws-release-assets/README.md`)
and serves that version from this directory. A mount holding a different
version serves nothing. Attached hosts select and
verify the digest for their architecture; a connected-AWS release publishes
`linux/amd64`, which is what AWS node classes consume.

The build workflow uploads this directory as a GitHub Actions artifact.
The [release-assets workflow](../aws-release-assets/README.md) publishes the
selected release for customer nodes.

The same Dockerfile also owns the `agent-runtime` image used by the canonical
local Compose stack. That image unpacks the release into the same layout and
runs the production agent with the Docker client and host preflight tools; it
uses the host Docker socket to launch the production worker image as a sibling
container.
