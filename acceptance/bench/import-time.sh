#!/bin/sh
# Times the runner's imports and the bench app's import in a fresh
# python:3.12-slim container with a managed runtime mounted as agents mount it.
# Usage: import-time.sh <runtime dir> [runs]
set -eu
runtime=$1
runs=${2:-3}
app=${LCBENCH_STATE_ROOT:-/tmp/lcbench}/app
i=0
while [ "$i" -lt "$runs" ]; do
  docker run --rm -v "$runtime:/opt/lazycloud/runtime:ro" -v "$app:/workspace:ro" -w /workspace \
    -e PYTHONPATH=/opt/lazycloud/runtime:/workspace python:3.12-slim python3 -c \
    "import time; t = time.perf_counter(); import runner.protocol; a = time.perf_counter(); import benchapp; print('runner_ms', round((a - t) * 1000), 'app_ms', round((time.perf_counter() - a) * 1000))"
  i=$((i + 1))
done
