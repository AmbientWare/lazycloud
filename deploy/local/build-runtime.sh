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
# LAZYCLOUD_RUNTIME_PLATFORM selects the wheels; the agent bundle sets it per architecture.
platform="${LAZYCLOUD_RUNTIME_PLATFORM:-x86_64-manylinux_2_28}"

mkdir -p "$out"
out="$(cd "$out" && pwd)"
work="$(mktemp -d "$out/.build.XXXXXX")"
trap 'rm -rf "$work"' EXIT

# The workspace packages are pure Python source trees; building their wheels
# lets every install below keep --only-binary for all packages.
# setuptools leaves build/ in the package directory; it is removed so the
# source tree stays as it was.
for package in shared lazycloud runner; do
  "$uv" build --quiet --wheel --out-dir "$work/wheels" "$root/python/$package"
  rm -rf "$root/python/$package/build"
done

# Third-party versions and wheel digests follow uv.lock, so one commit builds
# the same runtime and a tampered download fails. The workspace wheels are
# pinned to the digests just built.
"$uv" export --quiet --frozen --no-dev --no-emit-workspace \
  --project "$root" --package runner --package lazycloud-client >"$work/requirements.txt"
for wheel in "$work"/wheels/*.whl; do
  echo "$wheel --hash=sha256:$(sha256sum "$wheel" | cut -d' ' -f1)" >>"$work/requirements.txt"
done

failed=()
for version in "${versions[@]}"; do
  target="$work/$version"
  echo "building runtime for Python $version" >&2
  if ! "$uv" pip install --quiet --no-cache --target "$target" \
    --python-version "$version" --python-platform "$platform" \
    --only-binary :all: --require-hashes --requirements "$work/requirements.txt"; then
    failed+=("$version")
    continue
  fi
  # Console scripts carry the build host's interpreter path, direct_url.json
  # names the temporary wheel directory and uv_cache.json the install time;
  # none belongs in the mounted runtime, which must build the same each time.
  rm -rf "$target/bin"
  find "$target" -path '*.dist-info/RECORD' -exec sed -i -E '/\.dist-info\/(direct_url|uv_cache)\.json,/d' {} +
  find "$target" -path '*.dist-info/*' \( -name direct_url.json -o -name uv_cache.json \) -delete
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
