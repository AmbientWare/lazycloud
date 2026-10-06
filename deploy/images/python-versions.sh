#!/bin/sh
# Prints the Python minors the platform serves, which docker-bake.hcl lists.
set -eu
docker buildx bake -f "$(dirname "$0")/docker-bake.hcl" --progress=quiet --print _python | jq -er '.target._python.args.PYTHON_VERSIONS | select(length > 0)'
