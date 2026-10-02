#!/bin/sh
# Runs a command with one platform's own SDK environment, from the app directory.
# Usage: sdk.sh ref|new <command...>   ("python" and "lazycloud" resolve to that tree's venv)
set -eu
bench=$(cd "$(dirname "$0")" && pwd)
root=$(cd "$bench/../.." && pwd)
state=${LCBENCH_STATE_ROOT:-/tmp/lcbench}
target=$1
shift
case "$target" in
  ref)
    venv=${LCBENCH_REF:-/tmp/lc-ref}/.venv
    export LAZYCLOUD_ENDPOINT=http://lazycloud.localhost:38000
    export LAZYCLOUD_WORKSPACE=${LCBENCH_WORKSPACE:-tenant-customer} LCBENCH_MACHINE=lcbench-agent
    export LCBENCH_CALLBACK_URL=http://host.docker.internal:29999/hook
    LAZYCLOUD_TOKEN=$(cat "${LCBENCH_TOKEN_FILE:-$state/ref-token}")
    ;;
  new)
    venv=$root/.venv
    export LAZYCLOUD_ENDPOINT=http://127.0.0.1:28080
    export LAZYCLOUD_WORKSPACE=${LCBENCH_WORKSPACE:-dev} LCBENCH_CALLBACK_URL=http://127.0.0.1:29999/hook
    LAZYCLOUD_TOKEN=$(cat "${LCBENCH_TOKEN_FILE:-$state/new/token}")
    ;;
  *) echo "usage: $0 ref|new <command...>" >&2; exit 2 ;;
esac
export LAZYCLOUD_TOKEN LCBENCH_TARGET=$target
[ "${LCBENCH_WORKSPACE:-}" = none ] && unset LAZYCLOUD_WORKSPACE
# A private profile directory keeps the user's own CLI profiles out of the run.
export HOME=$state/home-$target XDG_CONFIG_HOME=$state/home-$target/.config
mkdir -p "$HOME" "$state/app"
cp "$bench/app/benchapp.py" "$state/app/benchapp.py"
cd "$state/app"
export PATH="$venv/bin:$PATH" PYTHONPATH="$state/app:$bench"
exec "$@"
