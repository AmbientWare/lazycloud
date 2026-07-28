# Agent Artifacts

This build produces standalone Linux `amd64` and `arm64` agent executables.
It uses Python 3.12 on glibc 2.31 so the artifacts run on Amazon Linux 2023
without a host Python installation.

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
Mount the output root read-only at `LAZYCLOUD_AGENT_BINARY_DIR`. Set
`LAZYCLOUD_AGENT_BINARY_VERSION` to the directory name and
`LAZYCLOUD_AGENT_BINARY_SHA256_BY_ARCH` to a JSON object containing the
manifest's `linux/amd64` and `linux/arm64` digests. Attached hosts select and
verify the digest for their architecture; AWS node classes consume `amd64`.

The release workflow uploads this directory as a GitHub Actions artifact. It
does not publish the executables to a package index, object store, or release.

The same Dockerfile also owns the `agent-runtime` image used by the canonical
local Compose stack. That image runs the standalone production agent with the
Docker client, Tailscale transport binaries, and host preflight tools; it uses
the host Docker socket to launch the production worker image as a sibling
container.
