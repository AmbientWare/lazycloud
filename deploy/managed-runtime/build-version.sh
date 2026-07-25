#!/usr/bin/env bash
set -euo pipefail

output_root="${1:?output root is required}"
architecture="${2:?target architecture is required}"
python_version="$(python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
python_tag="${python_version//./}"
requirements="/app/deploy/managed-runtime/locks/python${python_tag}-linux-${architecture}.txt"
managed_root="/tmp/managed-runtime-${python_version}-${architecture}/managed"
dependency_root="/tmp/managed-runtime-${python_version}-${architecture}/dependencies"
build_tools="/app/deploy/managed-runtime/locks/build-tools.txt"

case "$architecture" in
  amd64) python_platform="x86_64-manylinux_2_28" ;;
  arm64) python_platform="aarch64-manylinux_2_28" ;;
  *) echo "unsupported managed runtime architecture: $architecture" >&2; exit 1 ;;
esac

uv pip install --python /usr/local/bin/python --strict --only-binary :all: \
  --require-hashes --requirements "$build_tools"
python deploy/managed-runtime/locks.py check \
  --python-version "$python_version" \
  --architecture "$architecture"
uv pip install --python /usr/local/bin/python --no-deps --no-build-isolation \
  --target "$managed_root" \
  /app/packages/shared \
  /app/packages/foundation \
  /app/packages/lazycloud \
  /app/packages/runner
uv pip install --python /usr/local/bin/python --strict --only-binary :all: \
  --require-hashes --python-platform "$python_platform" \
  --target "$dependency_root" --requirements "$requirements"

if [[ "$(dpkg --print-architecture)" == "$architecture" ]]; then
  PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$managed_root:$dependency_root" python -c \
    'import foundation, runner, shared, lazycloud; print("managed runtime imports ok")'
fi

PYTHONPATH="/app/packages/shared/src" \
  python /app/deploy/managed-runtime/build.py build \
  --packages-root /app/packages \
  --managed-root "$managed_root" \
  --dependency-root "$dependency_root" \
  --requirements "$requirements" \
  --architecture "$architecture" \
  --output-root "$output_root"
