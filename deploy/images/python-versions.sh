#!/usr/bin/env bash
# Prints the Python minors the platform serves, which docker-bake.hcl lists.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
docker buildx bake -f deploy/images/docker-bake.hcl --progress=quiet --print _python |
  jq -er '.target._python.args.PYTHON_VERSIONS'
