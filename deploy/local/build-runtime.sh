#!/usr/bin/env bash
# Build the managed Python runtime an agent bind-mounts read-only at
# /opt/lazycloud/runtime with PYTHONPATH pointing there, one directory per
# Python version. It holds the runner, the shared contracts and the SDK, because
# user modules import lazycloud at top level to declare their apps.
#
# Usage: deploy/local/build-runtime.sh [out_dir] [python_versions...]
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
uv="${UV:-$(command -v uv || echo "$HOME/.local/bin/uv")}"
out="${1:-$root/.lazycloud/runtime}"
shift || true
versions=("$@")
if [[ ${#versions[@]} -eq 0 ]]; then
  versions=(3.10 3.11 3.12 3.13 3.14)
fi
platform="x86_64-manylinux_2_28"

mkdir -p "$out"
out="$(cd "$out" && pwd)"
work="$(mktemp -d "$out/.build.XXXXXX")"
trap 'rm -rf "$work"' EXIT

# The workspace packages are pure Python source trees; building their wheels
# lets every install below keep --only-binary for all packages.
for package in shared lazycloud runner; do
  "$uv" build --quiet --wheel --out-dir "$work/wheels" "$root/python/$package"
done

failed=()
for version in "${versions[@]}"; do
  target="$work/$version"
  echo "building runtime for Python $version" >&2
  if ! "$uv" pip install --quiet --no-cache --target "$target" \
    --python-version "$version" --python-platform "$platform" \
    --only-binary :all: "$work"/wheels/*.whl; then
    failed+=("$version")
    continue
  fi
  # Console scripts carry the build host's interpreter path, and direct_url.json
  # names the temporary wheel directory; neither belongs in the mounted runtime.
  rm -rf "$target/bin"
  find "$target" -name direct_url.json -path '*.dist-info/*' -delete
  find "$target" -name __pycache__ -type d -prune -exec rm -rf {} +
  find "$target" -name '*.py[co]' -delete

  previous="$work/previous-$version"
  if [[ -e "$out/$version" ]]; then
    mv "$out/$version" "$previous"
  fi
  mv "$target" "$out/$version"
  rm -rf "$previous"
  echo "built $out/$version" >&2
done

if [[ ${#failed[@]} -gt 0 ]]; then
  echo "runtime build failed for Python ${failed[*]}" >&2
  exit 1
fi
