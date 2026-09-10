# Agent Artifacts

This build produces standalone Linux `amd64` and `arm64` agent executables.
It uses uv-managed Python 3.12 on AlmaLinux 8 with glibc 2.28 so the artifacts
run on Amazon Linux 2023 without a host Python installation.

Build and stage both architectures from the repository root:

```sh
uv run --no-project python deploy/agent-binary/build.py build \
  --version 0.1.0 \
  --output dist/agent-binarys
```

The output is immutable publication input, not an external publication:

```text
dist/agent-binarys/
  0.1.0/
    manifest.json
    lazycloud-agent-linux-amd64
    lazycloud-agent-linux-arm64
```

The Docker build runs `--help` on each target executable in both its build
environment and Amazon Linux 2023 before export. The stager then computes each
SHA-256 digest and records it in `manifest.json`.
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

The release workflow uploads this directory as a GitHub Actions artifact. It
does not publish the executables to a package index, object store, or release.

The same Dockerfile also owns the `agent-runtime` image used by the canonical
local Compose stack. That image runs the standalone production agent with the
Docker client, WireGuard tools, and host preflight tools; it uses
the host Docker socket to launch the production worker image as a sibling
container.
