#!/bin/sh
# Runs a command with the benchmark stack's SDK environment, from the app
# directory. "python" and "lazycloud" resolve to $LCBENCH_TREE's venv.
# Usage: sdk.sh <command...>
set -eu
bench=$(cd "$(dirname "$0")" && pwd)
tree=$(cd "${LCBENCH_TREE:-$bench/../..}" && pwd)
root=${LCBENCH_ROOT:-/tmp/lcbench}
state=${LCBENCH_STATE:-$root/stack}
export LCBENCH_ROOT="$root" LCBENCH_STATE="$state"
export LAZYCLOUD_ENDPOINT=http://127.0.0.1:28080
export LAZYCLOUD_WORKSPACE="${LCBENCH_WORKSPACE:-dev}" LCBENCH_CALLBACK_URL=http://127.0.0.1:29999/hook
LAZYCLOUD_TOKEN=$(cat "$state/token")
export LAZYCLOUD_TOKEN
# A private profile directory keeps the user's own CLI profiles out of the run.
export HOME="$root/home" XDG_CONFIG_HOME="$root/home/.config"
mkdir -p "$HOME" "$root/app"
cp "$bench/app/benchapp.py" "$root/app/benchapp.py"
cd "$root/app"
export PATH="$tree/.venv/bin:$PATH" PYTHONPATH="$root/app:$bench"
exec "$@"
