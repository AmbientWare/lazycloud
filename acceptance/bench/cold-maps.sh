#!/bin/sh
# Runs a map of n inputs on new source, so every container starts cold, after
# the host has no workload container left, runs times.
# Usage: cold-maps.sh ref|new n runs
set -eu
bench=$(cd "$(dirname "$0")" && pwd)
target=$1 n=$2 runs=$3
i=1
while [ "$i" -le "$runs" ]; do
  until [ "$(cd "$bench" && python3 -c "import sample; print(sample.snapshot('$target').workloads[0])")" = 0 ]; do
    sleep 3
  done
  "$bench/sdk.sh" "$target" python -m scenarios map "$n" "cold$i" >/dev/null 2>&1
  i=$((i + 1))
done
